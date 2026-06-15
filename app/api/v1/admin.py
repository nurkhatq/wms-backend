from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
import datetime
import redis.asyncio as aioredis
from app.database import get_db
from app.redis_client import get_redis
from app.models.kaspi_order import KaspiOrder
from app.models.kaspi_order_event import KaspiOrderEvent
from app.config import settings

router = APIRouter(prefix="/admin", tags=["admin"])

SYNC_TOKEN_HEADER = "X-Sync-Token"

PICKUP_POINT_MAP = {"PP1": 1, "PP2": 2, "PP5": 5}


def _warehouse_from_pickup(pickup_point_id: str | None) -> int:
    if not pickup_point_id:
        return 2
    for suffix, wh_id in PICKUP_POINT_MAP.items():
        if str(pickup_point_id).endswith(f"_{suffix}") or str(pickup_point_id) == suffix:
            return wh_id
    return 2


class KaspiOrderItem(BaseModel):
    kaspi_order_code: str
    kaspi_order_id: str | None = None
    kaspi_status: str
    kaspi_state: str | None = None
    delivery_mode: str | None = None
    pickup_point_id: str | None = None
    origin_address_b64: str | None = None
    total_price: float = 0
    customer_name: str | None = None
    customer_phone: str | None = None
    creation_date: str | None = None
    delivery_slot_from: str | None = None
    delivery_slot_to: str | None = None
    waybill_number: str | None = None
    express: bool = False
    assembled: bool = False
    cancellation_reason: str | None = None
    products_json: list = []


class SyncPayload(BaseModel):
    orders: list[KaspiOrderItem]
    synced_at: str | None = None


@router.post("/sync")
async def sync_kaspi_orders(
    payload: SyncPayload,
    db: AsyncSession = Depends(get_db),
    redis: aioredis.Redis = Depends(get_redis),
    x_sync_token: str = Header(alias="x-sync-token"),
):
    if x_sync_token != settings.secret_key:
        raise HTTPException(status_code=403, detail="Invalid sync token")

    now = datetime.datetime.now(datetime.timezone.utc)
    upserted = 0
    events_added = 0

    for item in payload.orders:
        code = item.kaspi_order_code
        wh_id = _warehouse_from_pickup(item.pickup_point_id)

        creation_dt = None
        if item.creation_date:
            try:
                creation_dt = datetime.datetime.fromisoformat(item.creation_date)
            except Exception:
                pass

        existing = await db.scalar(
            select(KaspiOrder).where(KaspiOrder.kaspi_order_code == code)
        )

        if existing is None:
            order = KaspiOrder(
                kaspi_order_code=code,
                kaspi_order_id=item.kaspi_order_id,
                warehouse_id=wh_id,
                kaspi_status=item.kaspi_status,
                kaspi_state=item.kaspi_state,
                delivery_mode=item.delivery_mode,
                pickup_point_id=item.pickup_point_id,
                origin_address_b64=item.origin_address_b64,
                total_price=item.total_price,
                customer_name=item.customer_name,
                customer_phone=item.customer_phone,
                creation_date=creation_dt,
                delivery_slot_from=item.delivery_slot_from,
                delivery_slot_to=item.delivery_slot_to,
                waybill_number=item.waybill_number,
                express=item.express,
                assembled=item.assembled,
                is_cancelling=item.kaspi_status == "CANCELLING",
                cancellation_reason=item.cancellation_reason,
                products_json=item.products_json,
                last_polled_at=now,
            )
            db.add(order)
            await db.flush()
            db.add(KaspiOrderEvent(
                order_id=order.id, event_type="FIRST_SEEN",
                new_value=item.kaspi_status, triggered_by="sync",
                warehouse_id=wh_id,
            ))
            events_added += 1
        else:
            old_status = existing.kaspi_status
            old_assembled = existing.assembled

            if old_status != item.kaspi_status:
                db.add(KaspiOrderEvent(
                    order_id=existing.id, event_type="STATUS_CHANGE",
                    old_value=old_status, new_value=item.kaspi_status,
                    triggered_by="sync", warehouse_id=existing.warehouse_id,
                ))
                existing.last_status_changed_at = now
                events_added += 1
                if item.kaspi_status == "CANCELLING" and not existing.is_cancelling:
                    existing.cancelling_detected_at = now
                    db.add(KaspiOrderEvent(
                        order_id=existing.id, event_type="CANCELLING_DETECTED",
                        new_value=item.cancellation_reason, triggered_by="sync",
                        warehouse_id=existing.warehouse_id,
                    ))
                    await redis.sadd(f"wms:cancelling:orders:{existing.warehouse_id}", code)
                    events_added += 1

            if not old_assembled and item.assembled:
                db.add(KaspiOrderEvent(
                    order_id=existing.id, event_type="ASSEMBLED",
                    old_value="false", new_value="true",
                    triggered_by="sync", warehouse_id=existing.warehouse_id,
                ))
                events_added += 1

            existing.kaspi_status = item.kaspi_status
            existing.kaspi_state = item.kaspi_state
            existing.assembled = item.assembled
            existing.is_cancelling = item.kaspi_status == "CANCELLING"
            existing.cancellation_reason = item.cancellation_reason
            existing.last_polled_at = now
            if item.waybill_number:
                existing.waybill_number = item.waybill_number

        # Redis cache
        await redis.hset(f"wms:order:status:{code}", mapping={
            "status": item.kaspi_status,
            "assembled": "1" if item.assembled else "0",
            "warehouse_id": str(wh_id),
            "is_cancelling": "1" if item.kaspi_status == "CANCELLING" else "0",
            "last_updated": now.isoformat(),
        })
        await redis.expire(f"wms:order:status:{code}", 600)
        upserted += 1

    await db.commit()

    for wh_id in PICKUP_POINT_MAP.values():
        await redis.set(f"wms:poller:last_run:{wh_id}", now.isoformat(), ex=600)

    return {"upserted": upserted, "events": events_added, "synced_at": now.isoformat()}


@router.get("/poller/status")
async def poller_status(
    redis: aioredis.Redis = Depends(get_redis),
    x_sync_token: str = Header(alias="x-sync-token"),
):
    if x_sync_token != settings.secret_key:
        raise HTTPException(status_code=403, detail="Invalid sync token")
    result = {}
    for wh_id, code in [(1, "PP1"), (2, "PP2"), (5, "PP5")]:
        last = await redis.get(f"wms:poller:last_run:{wh_id}")
        result[code] = last or "NEVER"
    return result
