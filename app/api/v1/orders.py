from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from app.database import get_db
from app.models.kaspi_order import KaspiOrder
from app.models.kaspi_order_event import KaspiOrderEvent
from app.models.user import User
from app.api.deps import get_current_user

router = APIRouter(prefix="/orders", tags=["orders"])


@router.get("")
async def get_orders(
    status: str | None = Query(None),
    assembled: bool | None = Query(None),
    moysklad_status: str | None = Query(None),
    page: int = Query(0, ge=0),
    page_size: int = Query(50, le=200),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    q = select(KaspiOrder).where(KaspiOrder.warehouse_id == user.warehouse_id)
    if status:
        q = q.where(KaspiOrder.kaspi_status == status)
    if assembled is not None:
        q = q.where(KaspiOrder.assembled == assembled)
    if moysklad_status:
        q = q.where(KaspiOrder.moysklad_status == moysklad_status)
    q = q.order_by(KaspiOrder.creation_date.desc()).offset(page * page_size).limit(page_size)

    rows = (await db.scalars(q)).all()
    return [_order_to_dict(o) for o in rows]


@router.get("/cancelling")
async def get_cancelling(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    rows = (await db.scalars(
        select(KaspiOrder).where(
            KaspiOrder.warehouse_id == user.warehouse_id,
            KaspiOrder.is_cancelling == True,
        ).order_by(KaspiOrder.cancelling_detected_at.desc())
    )).all()
    return [_order_to_dict(o) for o in rows]


@router.get("/{order_code}")
async def get_order(
    order_code: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    order = await db.scalar(
        select(KaspiOrder).where(
            KaspiOrder.kaspi_order_code == order_code,
            KaspiOrder.warehouse_id == user.warehouse_id,
        )
    )
    if not order:
        return {"error": "not_found"}
    events = (await db.scalars(
        select(KaspiOrderEvent)
        .where(KaspiOrderEvent.order_id == order.id)
        .order_by(KaspiOrderEvent.created_at.desc())
        .limit(20)
    )).all()
    result = _order_to_dict(order)
    result["events"] = [
        {"type": e.event_type, "old": e.old_value, "new": e.new_value,
         "by": e.triggered_by, "at": e.created_at.isoformat() if e.created_at else None}
        for e in events
    ]
    return result


def _order_to_dict(o: KaspiOrder) -> dict:
    return {
        "order_code": o.kaspi_order_code,
        "kaspi_status": o.kaspi_status,
        "kaspi_state": o.kaspi_state,
        "customer_name": o.customer_name,
        "customer_phone": o.customer_phone,
        "total_price": float(o.total_price or 0),
        "assembled": o.assembled,
        "express": o.express,
        "is_cancelling": o.is_cancelling,
        "cancellation_reason": o.cancellation_reason,
        "moysklad_status": o.moysklad_status,
        "delivery_slot_from": o.delivery_slot_from,
        "delivery_slot_to": o.delivery_slot_to,
        "waybill_number": o.waybill_number,
        "creation_date": o.creation_date.isoformat() if o.creation_date else None,
        "warehouse_id": o.warehouse_id,
    }
