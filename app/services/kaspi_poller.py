import logging
import datetime
from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from app.database import AsyncSessionLocal
from app.redis_client import get_redis_pool
from app.models.kaspi_order import KaspiOrder
from app.models.kaspi_order_event import KaspiOrderEvent
from app.services.kaspi_client import fetch_all_active

logger = logging.getLogger("wms.poller")

# pickupPointId suffix → warehouse_id
PICKUP_POINT_MAP = {
    "PP1": 1,
    "PP2": 2,
    "PP5": 5,
}


def _warehouse_from_pickup(pickup_point_id: str | None) -> int | None:
    if not pickup_point_id:
        return None
    for suffix, wh_id in PICKUP_POINT_MAP.items():
        if pickup_point_id.endswith(f"_{suffix}") or pickup_point_id == suffix:
            return wh_id
    return None


def _parse_order(raw: dict) -> dict:
    a = raw.get("attributes", {})
    kd = a.get("kaspiDelivery") or {}
    slot = a.get("deliverySlot") or {}
    pickup_id = a.get("pickupPointId")
    origin = a.get("originAddress") or {}
    customer = a.get("customer") or {}

    creation_ms = a.get("creationDate")
    creation_dt = (
        datetime.datetime.fromtimestamp(creation_ms / 1000, tz=datetime.timezone.utc)
        if creation_ms else None
    )

    return {
        "kaspi_order_code": str(a.get("code", "")),
        "kaspi_order_id": raw.get("id"),
        "kaspi_status": a.get("status", "UNKNOWN"),
        "kaspi_state": a.get("state"),
        "delivery_mode": a.get("deliveryMode"),
        "pickup_point_id": pickup_id,
        "origin_address_b64": origin.get("id"),
        "warehouse_id": _warehouse_from_pickup(pickup_id) or 2,
        "total_price": float(a.get("totalPrice") or 0),
        "customer_name": customer.get("name") or customer.get("firstName"),
        "customer_phone": customer.get("cellPhone"),
        "creation_date": creation_dt,
        "delivery_slot_from": slot.get("from"),
        "delivery_slot_to": slot.get("to"),
        "waybill_number": kd.get("waybillNumber"),
        "express": bool(kd.get("express", False)),
        "assembled": bool(a.get("assembled", False)),
        "is_cancelling": a.get("status") == "CANCELLING",
        "cancellation_reason": a.get("cancellationReason"),
        "last_polled_at": datetime.datetime.now(datetime.timezone.utc),
        "products_json": [],
    }


async def run_poll():
    logger.info("Kaspi poll started")
    try:
        raw_orders = await fetch_all_active()
        logger.info(f"Fetched {len(raw_orders)} orders from Kaspi")
    except Exception as e:
        logger.error(f"Failed to fetch from Kaspi: {e}")
        return

    redis = get_redis_pool()
    now = datetime.datetime.now(datetime.timezone.utc)

    async with AsyncSessionLocal() as db:
        for raw in raw_orders:
            parsed = _parse_order(raw)
            code = parsed["kaspi_order_code"]
            if not code:
                continue

            # Load existing order
            existing = await db.scalar(
                select(KaspiOrder).where(KaspiOrder.kaspi_order_code == code)
            )

            if existing is None:
                order = KaspiOrder(**parsed)
                db.add(order)
                await db.flush()
                order_id = order.id
                db.add(KaspiOrderEvent(
                    order_id=order_id,
                    event_type="FIRST_SEEN",
                    new_value=parsed["kaspi_status"],
                    triggered_by="poller",
                    warehouse_id=parsed["warehouse_id"],
                ))
            else:
                order_id = existing.id
                old_status = existing.kaspi_status
                new_status = parsed["kaspi_status"]
                old_assembled = existing.assembled
                new_assembled = parsed["assembled"]

                # Status changed
                if old_status != new_status:
                    db.add(KaspiOrderEvent(
                        order_id=order_id,
                        event_type="STATUS_CHANGE",
                        old_value=old_status,
                        new_value=new_status,
                        triggered_by="poller",
                        warehouse_id=existing.warehouse_id,
                    ))
                    existing.last_status_changed_at = now
                    if new_status == "CANCELLING" and not existing.is_cancelling:
                        existing.cancelling_detected_at = now
                        db.add(KaspiOrderEvent(
                            order_id=order_id,
                            event_type="CANCELLING_DETECTED",
                            new_value=parsed.get("cancellation_reason"),
                            triggered_by="poller",
                            warehouse_id=existing.warehouse_id,
                        ))
                        await redis.sadd(
                            f"wms:cancelling:orders:{existing.warehouse_id}", code
                        )

                # assembled false→true
                if not old_assembled and new_assembled:
                    db.add(KaspiOrderEvent(
                        order_id=order_id,
                        event_type="ASSEMBLED",
                        old_value="false",
                        new_value="true",
                        triggered_by="poller",
                        warehouse_id=existing.warehouse_id,
                    ))

                for field, val in parsed.items():
                    if field not in ("products_json",):
                        setattr(existing, field, val)

            # Update Redis status cache
            wh_id = parsed["warehouse_id"]
            await redis.hset(f"wms:order:status:{code}", mapping={
                "status": parsed["kaspi_status"],
                "assembled": "1" if parsed["assembled"] else "0",
                "warehouse_id": str(wh_id),
                "is_cancelling": "1" if parsed["is_cancelling"] else "0",
                "last_updated": now.isoformat(),
                "customer_name": parsed.get("customer_name") or "",
                "total_price": str(parsed.get("total_price") or 0),
                "express": "1" if parsed.get("express") else "0",
            })
            await redis.expire(f"wms:order:status:{code}", 900)

        await db.commit()

    # Update poller heartbeat per warehouse
    for wh_id in PICKUP_POINT_MAP.values():
        await redis.set(f"wms:poller:last_run:{wh_id}", now.isoformat(), ex=600)

    logger.info(f"Kaspi poll complete: {len(raw_orders)} orders processed")
