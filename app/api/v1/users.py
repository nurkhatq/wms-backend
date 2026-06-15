from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func
from app.database import get_db
from app.models.user import User
from app.models.kaspi_order import KaspiOrder
from app.models.scan_session import ScanSession
from app.api.deps import get_current_user
from app.services.auth_service import hash_password

router = APIRouter(prefix="/users", tags=["users"])


def _require_admin(user: User = Depends(get_current_user)):
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="Admin only")
    return user


class UserCreate(BaseModel):
    username: str
    password: str
    full_name: str
    warehouse_id: int
    role: str = "operator"


class UserUpdate(BaseModel):
    full_name: str | None = None
    password: str | None = None
    warehouse_id: int | None = None
    role: str | None = None
    is_active: bool | None = None


@router.get("")
async def list_users(
    db: AsyncSession = Depends(get_db),
    _: User = Depends(_require_admin),
):
    rows = (await db.scalars(select(User).order_by(User.warehouse_id, User.id))).all()
    return [_user_dict(u) for u in rows]


@router.post("", status_code=201)
async def create_user(
    body: UserCreate,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(_require_admin),
):
    existing = await db.scalar(select(User).where(User.username == body.username))
    if existing:
        raise HTTPException(status_code=409, detail="Username already exists")
    user = User(
        username=body.username,
        password_hash=hash_password(body.password),
        full_name=body.full_name,
        warehouse_id=body.warehouse_id,
        role=body.role,
        is_active=True,
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return _user_dict(user)


@router.patch("/{user_id}")
async def update_user(
    user_id: int,
    body: UserUpdate,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(_require_admin),
):
    user = await db.get(User, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    if body.full_name is not None:
        user.full_name = body.full_name
    if body.password:
        user.password_hash = hash_password(body.password)
    if body.warehouse_id is not None:
        user.warehouse_id = body.warehouse_id
    if body.role is not None:
        user.role = body.role
    if body.is_active is not None:
        user.is_active = body.is_active
    await db.commit()
    await db.refresh(user)
    return _user_dict(user)


@router.delete("/{user_id}", status_code=204)
async def delete_user(
    user_id: int,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(_require_admin),
):
    if user_id == admin.id:
        raise HTTPException(status_code=400, detail="Cannot delete yourself")
    user = await db.get(User, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    await db.delete(user)
    await db.commit()


@router.get("/stats")
async def get_stats(
    db: AsyncSession = Depends(get_db),
    _: User = Depends(_require_admin),
):
    from sqlalchemy import and_
    import datetime

    now = datetime.datetime.now(datetime.timezone.utc)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)

    total_orders = await db.scalar(select(func.count()).select_from(KaspiOrder))
    assembled = await db.scalar(select(func.count()).select_from(KaspiOrder).where(KaspiOrder.assembled == True))
    cancelling = await db.scalar(select(func.count()).select_from(KaspiOrder).where(KaspiOrder.is_cancelling == True))

    by_warehouse = {}
    for wh_id in [1, 2, 5]:
        cnt = await db.scalar(
            select(func.count()).select_from(KaspiOrder)
            .where(KaspiOrder.warehouse_id == wh_id)
        )
        by_warehouse[str(wh_id)] = cnt or 0

    sessions_today = await db.scalar(
        select(func.count()).select_from(ScanSession)
        .where(ScanSession.started_at >= today_start)
    )

    return {
        "total_orders": total_orders or 0,
        "assembled": assembled or 0,
        "cancelling": cancelling or 0,
        "by_warehouse": by_warehouse,
        "sessions_today": sessions_today or 0,
    }


def _user_dict(u: User) -> dict:
    return {
        "id": u.id,
        "username": u.username,
        "full_name": u.full_name,
        "warehouse_id": u.warehouse_id,
        "role": u.role,
        "is_active": u.is_active,
        "last_login_at": u.last_login_at.isoformat() if u.last_login_at else None,
    }
