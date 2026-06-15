from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, update
import datetime
import redis.asyncio as aioredis
from app.database import get_db
from app.redis_client import get_redis
from app.models.scan_session import ScanSession
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
