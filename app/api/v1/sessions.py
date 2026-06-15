from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, update
import datetime
import redis.asyncio as aioredis
from app.database import get_db
from app.redis_client import get_redis
from app.models.scan_session import ScanSession
from app.models.scanned_order import ScannedOrder
from app.models.kaspi_order import KaspiOrder
from app.models.user import User
from app.api.deps import get_current_user

router = APIRouter(prefix="/sessions", tags=["sessions"])


class SessionCreate(BaseModel):
    notes: str | None = None


@router.post("")
async def create_session(
    body: SessionCreate,
    db: AsyncSession = Depends(get_db),
    redis: aioredis.Redis = Depends(get_redis),
    user: User = Depends(get_current_user),
):
    # One active session per warehouse
    existing = await db.scalar(
        select(ScanSession).where(
            ScanSession.warehouse_id == user.warehouse_id,
            ScanSession.status == "ACTIVE",
        )
    )
    if existing:
        raise HTTPException(status_code=409, detail=f"Активная сессия уже есть: {existing.batch_id}")

    session = ScanSession(
        warehouse_id=user.warehouse_id,
        started_by=user.id,
        tsd_device_id=user.tsd_device_id,
        notes=body.notes,
    )
    db.add(session)
    await db.flush()
    await db.commit()
    await db.refresh(session)

    await redis.set(
        f"wms:session:active:{user.warehouse_id}",
        f"{session.batch_id}:{user.username}",
        ex=86400,
    )
    return {"batch_id": session.batch_id, "status": session.status, "started_at": session.started_at.isoformat()}


@router.get("/active")
async def get_active(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    session = await db.scalar(
        select(ScanSession).where(
            ScanSession.warehouse_id == user.warehouse_id,
            ScanSession.status == "ACTIVE",
        )
    )
    if not session:
        return None
    return {"batch_id": session.batch_id, "status": session.status,
            "order_count": session.order_count, "started_at": session.started_at.isoformat()}


@router.patch("/{batch_id}")
async def update_session(
    batch_id: str,
    status: str,
    db: AsyncSession = Depends(get_db),
    redis: aioredis.Redis = Depends(get_redis),
    user: User = Depends(get_current_user),
):
    session = await db.scalar(
        select(ScanSession).where(ScanSession.batch_id == batch_id)
    )
    if not session:
        raise HTTPException(status_code=404)
    session.status = status
    if status in ("COMPLETED", "CANCELLED"):
        session.completed_at = datetime.datetime.now(datetime.timezone.utc)
        await redis.delete(f"wms:session:active:{session.warehouse_id}")
    await db.commit()
    return {"batch_id": batch_id, "status": status}


@router.get("")
async def list_sessions(
    page: int = 0,
    page_size: int = 20,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    from sqlalchemy.orm import aliased
    UserOp = aliased(User)
    q = (
        select(ScanSession, UserOp.full_name)
        .join(UserOp, ScanSession.started_by == UserOp.id)
        .order_by(ScanSession.started_at.desc())
        .offset(page * page_size).limit(page_size)
    )
    if user.role != "admin":
        q = q.where(ScanSession.warehouse_id == user.warehouse_id)
    rows = (await db.execute(q)).all()
    return [{
        "batch_id": s.batch_id,
        "status": s.status,
        "order_count": s.order_count,
        "started_at": s.started_at.isoformat(),
        "completed_at": s.completed_at.isoformat() if s.completed_at else None,
        "warehouse_id": s.warehouse_id,
        "user_name": full_name,
    } for s, full_name in rows]


@router.get("/{batch_id}/scans")
async def get_session_scans(
    batch_id: str,
    page: int = 0,
    page_size: int = 50,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """
    Return paginated list of ScannedOrder rows for a session,
    with customer_name and total_price joined from kaspi_orders.
    """
    session = await db.scalar(
        select(ScanSession).where(ScanSession.batch_id == batch_id)
    )
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    # Non-admin users can only see their own warehouse sessions
    if user.role != "admin" and session.warehouse_id != user.warehouse_id:
        raise HTTPException(status_code=403, detail="Forbidden")

    q = (
        select(
            ScannedOrder.order_code,
            ScannedOrder.scan_result,
            ScannedOrder.demand_status,
            ScannedOrder.demand_name,
            ScannedOrder.lock_holder,
            ScannedOrder.scanned_at,
            KaspiOrder.customer_name,
            KaspiOrder.total_price,
        )
        .outerjoin(KaspiOrder, ScannedOrder.order_id == KaspiOrder.id)
        .where(ScannedOrder.session_id == session.id)
        .order_by(ScannedOrder.scanned_at.desc())
        .offset(page * page_size)
        .limit(page_size)
    )
    rows = (await db.execute(q)).all()

    return [
        {
            "order_code": row.order_code,
            "customer_name": row.customer_name,
            "total_price": float(row.total_price) if row.total_price is not None else None,
            "scan_result": row.scan_result,
            "demand_status": row.demand_status,
            "demand_name": row.demand_name,
            "lock_holder": row.lock_holder,
            "scanned_at": row.scanned_at.isoformat() if row.scanned_at else None,
        }
        for row in rows
    ]
