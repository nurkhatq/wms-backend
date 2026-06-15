from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, update, func
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
    return {
        "batch_id": session.batch_id,
        "status": session.status,
        "order_count": session.order_count,
        "started_at": session.started_at.isoformat(),
    }


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
    return {
        "batch_id": session.batch_id,
        "status": session.status,
        "order_count": session.order_count,
        "started_at": session.started_at.isoformat(),
    }


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
    warehouse_id: int | None = None,
    user_search: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    from sqlalchemy.orm import aliased
    UserOp = aliased(User)

    q = (
        select(ScanSession, UserOp.full_name)
        .join(UserOp, ScanSession.started_by == UserOp.id)
        # Only completed sessions in history
        .where(ScanSession.status != "ACTIVE")
        .order_by(ScanSession.started_at.desc())
    )

    if user.role != "admin":
        q = q.where(ScanSession.warehouse_id == user.warehouse_id)
    elif warehouse_id:
        q = q.where(ScanSession.warehouse_id == warehouse_id)

    if user_search:
        q = q.where(UserOp.full_name.ilike(f"%{user_search}%"))

    if date_from:
        try:
            dt = datetime.datetime.fromisoformat(date_from)
            q = q.where(ScanSession.started_at >= dt)
        except ValueError:
            pass

    if date_to:
        try:
            dt = datetime.datetime.fromisoformat(date_to) + datetime.timedelta(days=1)
            q = q.where(ScanSession.started_at < dt)
        except ValueError:
            pass

    # Count total for pagination
    count_q = select(func.count()).select_from(q.subquery())
    total = (await db.scalar(count_q)) or 0

    rows = (await db.execute(q.offset(page * page_size).limit(page_size))).all()

    return {
        "total": total,
        "page": page,
        "page_size": page_size,
        "items": [{
            "batch_id": s.batch_id,
            "status": s.status,
            "order_count": s.order_count,
            "started_at": s.started_at.isoformat(),
            "completed_at": s.completed_at.isoformat() if s.completed_at else None,
            "warehouse_id": s.warehouse_id,
            "user_name": full_name,
        } for s, full_name in rows],
    }


@router.get("/{batch_id}/stats")
async def get_session_stats(
    batch_id: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    session = await db.scalar(
        select(ScanSession).where(ScanSession.batch_id == batch_id)
    )
    if not session:
        raise HTTPException(status_code=404)
    if user.role != "admin" and session.warehouse_id != user.warehouse_id:
        raise HTTPException(status_code=403)

    from sqlalchemy.orm import aliased
    UserOp = aliased(User)
    starter = await db.scalar(
        select(UserOp.full_name).where(UserOp.id == session.started_by)
    )

    # Counts by scan_result
    rows = (await db.execute(
        select(ScannedOrder.scan_result, func.count().label("cnt"))
        .where(ScannedOrder.session_id == session.id)
        .group_by(ScannedOrder.scan_result)
    )).all()
    by_result = {r.scan_result: r.cnt for r in rows}

    # Counts by demand_status
    drows = (await db.execute(
        select(ScannedOrder.demand_status, func.count().label("cnt"))
        .where(ScannedOrder.session_id == session.id, ScannedOrder.demand_status.isnot(None))
        .group_by(ScannedOrder.demand_status)
    )).all()
    by_demand = {r.demand_status: r.cnt for r in drows}

    total_scanned = sum(by_result.values())
    duration_sec = None
    if session.completed_at and session.started_at:
        duration_sec = int((session.completed_at - session.started_at).total_seconds())

    return {
        "batch_id": batch_id,
        "status": session.status,
        "warehouse_id": session.warehouse_id,
        "user_name": starter,
        "started_at": session.started_at.isoformat(),
        "completed_at": session.completed_at.isoformat() if session.completed_at else None,
        "duration_sec": duration_sec,
        "total_scanned": total_scanned,
        "by_result": by_result,
        "by_demand": by_demand,
    }


@router.get("/{batch_id}/scans")
async def get_session_scans(
    batch_id: str,
    page: int = 0,
    page_size: int = 50,
    scan_result: str | None = None,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    session = await db.scalar(
        select(ScanSession).where(ScanSession.batch_id == batch_id)
    )
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    if user.role != "admin" and session.warehouse_id != user.warehouse_id:
        raise HTTPException(status_code=403, detail="Forbidden")

    base = (
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
    )
    if scan_result:
        base = base.where(ScannedOrder.scan_result == scan_result)

    total = (await db.scalar(select(func.count()).select_from(base.subquery()))) or 0

    rows = (await db.execute(
        base.order_by(ScannedOrder.scanned_at.desc())
        .offset(page * page_size)
        .limit(page_size)
    )).all()

    return {
        "total": total,
        "page": page,
        "page_size": page_size,
        "items": [{
            "order_code": row.order_code,
            "customer_name": row.customer_name,
            "total_price": float(row.total_price) if row.total_price is not None else None,
            "scan_result": row.scan_result,
            "demand_status": row.demand_status,
            "demand_name": row.demand_name,
            "lock_holder": row.lock_holder,
            "scanned_at": row.scanned_at.isoformat() if row.scanned_at else None,
        } for row in rows],
    }
