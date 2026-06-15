from sqlalchemy import SmallInteger, String, Boolean, DateTime, ForeignKey
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func
from app.database import Base


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(50), unique=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String(200), nullable=False)
    full_name: Mapped[str] = mapped_column(String(150), nullable=False)
    warehouse_id: Mapped[int] = mapped_column(SmallInteger, ForeignKey("warehouses.id"), nullable=False)
    tsd_device_id: Mapped[int | None] = mapped_column(ForeignKey("tsd_devices.id"))
    role: Mapped[str] = mapped_column(String(20), default="operator")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[DateTime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_login_at: Mapped[DateTime | None] = mapped_column(DateTime(timezone=True))
