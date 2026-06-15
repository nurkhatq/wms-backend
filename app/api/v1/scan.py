from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, update
import redis.asyncio as aioredis
import datetime
from app.database import get_db
from app.redis_client import get_redis
from app.models.kaspi_order import KaspiOrder
from app.models.scanned_order import ScannedOrder
from app.models.scan_session import ScanSession
from app.models.user import User
from app.models.tsd_device import TsdDevice
from app.api.deps import get_current_user
from app.services import lock_service

router = APIRouter(prefix="/scan", tags=["scan"])


class LockRequest(BaseModel):
    order_code: str
    session_batch_id: str


@router.post("/lock")
async def scan_lock(
    body: LockRequest,
    db: AsyncSession = Depends(get_db),
    redis: aioredis.Redis = Depends(get_redis),
    user: User = Depends(get_current_user),
):
    order = await db.scalar(
        select(KaspiOrder).where(
            KaspiOrder.kaspi_order_code == body.order_code,
            KaspiOrder.warehouse_id == user.warehouse_id,
        )
    )
    if not order:
        return {"result": "NOT_FOUND", "order_code": body.order_code, "lock_acquired": False}

    if order.is_cancelling:
        return {
            "result": "CANCELLING",
            "order_code": body.order_code,
            "lock_acquired": False,
            "message": "Заказ в процессе отмены. Обратитесь к менеджеру.",
        }

    tsd_code = "TSD-UNKNOWN"
    if user.tsd_device_id:
        device = await db.scalar(select(TsdDevice).where(TsdDevice.id == user.tsd_device_id))
        if device:
            tsd_code = device.device_code
            await db.execute(
                update(TsdDevice).where(TsdDevice.id == device.id)
                .values(last_seen_at=datetime.datetime.now(datetime.timezone.utc))
            )

    acquired, holder = await lock_service.acquire(
        redis, body.order_code, tsd_code, user.full_name, body.session_batch_id
    )

    # Find session
    session = await db.scalar(
        select(ScanSession).where(
            ScanSession.batch_id == body.session_batch_id,
            ScanSession.status == "ACTIVE",
        )
    )

    if not acquired:
        if session:
            db.add(ScannedOrder(
                session_id=session.id, order_id=order.id,
                scanned_by=user.id, tsd_device_id=user.tsd_device_id,
                scan_result="ALREADY_LOCKED", lock_holder=holder,
            ))
            await db.commit()
        return {
            "result": "ALREADY_LOCKED",
            "order_code": body.order_code,
            "lock_holder": holder,
            "lock_acquired": False,
        }

    if session:
        db.add(ScannedOrder(
            session_id=session.id, order_id=order.id,
            scanned_by=user.id, tsd_device_id=user.tsd_device_id,
            scan_result="SUCCESS",
        ))
        await db.execute(
            update(ScanSession).where(ScanSession.id == session.id)
            .values(order_count=ScanSession.order_count + 1)
        )
    await db.commit()

    return {
        "result": "SUCCESS",
        "order_code": body.order_code,
        "lock_acquired": True,
        "lock_holder": holder,
        "order": {
            "kaspi_status": order.kaspi_status,
            "customer_name": order.customer_name,
            "total_price": float(order.total_price or 0),
            "assembled": order.assembled,
            "moysklad_status": order.moysklad_status,
            "express": order.express,
            "waybill_number": order.waybill_number,
        },
    }


@router.delete("/lock/{order_code}")
async def release_lock(
    order_code: str,
    redis: aioredis.Redis = Depends(get_redis),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    tsd_code = "TSD-UNKNOWN"
    if user.tsd_device_id:
        device = await db.scalar(select(TsdDevice).where(TsdDevice.id == user.tsd_device_id))
        if device:
            tsd_code = device.device_code
    released = await lock_service.release(redis, order_code, tsd_code)
    return {"released": released}


@router.get("/lock/{order_code}")
async def check_lock(
    order_code: str,
    redis: aioredis.Redis = Depends(get_redis),
    user: User = Depends(get_current_user),
):
    holder = await lock_service.get_holder(redis, order_code)
    return {"locked": holder is not None, "holder": holder}
