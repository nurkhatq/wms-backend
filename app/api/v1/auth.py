from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, update
from datetime import datetime, timezone
from app.database import get_db
from app.models.user import User
from app.models.scan_session import ScanSession
from app.services.auth_service import verify_password, create_access_token

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/login")
async def login(
    form: OAuth2PasswordRequestForm = Depends(),
    db: AsyncSession = Depends(get_db),
):
    user = await db.scalar(
        select(User).where(User.username == form.username, User.is_active == True)
    )
    if not user or not verify_password(form.password, user.password_hash):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Неверный логин или пароль")

    # Block login if this account already has an active session on another device
    active = await db.scalar(
        select(ScanSession).where(
            ScanSession.started_by == user.id,
            ScanSession.status == "ACTIVE",
        )
    )
    if active:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="На этом аккаунте уже открыта смена на другом устройстве. Завершите текущую смену перед входом.",
        )

    await db.execute(
        update(User).where(User.id == user.id)
        .values(last_login_at=datetime.now(timezone.utc))
    )
    await db.commit()

    token = create_access_token({
        "sub": str(user.id),
        "username": user.username,
        "warehouse_id": user.warehouse_id,
        "tsd_device_id": user.tsd_device_id,
        "role": user.role,
    })
    return {
        "access_token": token,
        "token_type": "bearer",
        "user": {
            "id": user.id,
            "username": user.username,
            "full_name": user.full_name,
            "warehouse_id": user.warehouse_id,
            "role": user.role,
        },
    }
