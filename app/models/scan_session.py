from sqlalchemy import SmallInteger, String, Boolean, DateTime, ForeignKey, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func, text
from app.database import Base


class ScanSession(Base):
    __tablename__ = "scan_sessions"

    id: Mapped[int] = mapped_column(primary_key=True)
    batch_id: Mapped[str] = mapped_column(UUID(as_uuid=False), unique=True, server_default=text("gen_random_uuid()"))
    warehouse_id: Mapped[int] = mapped_column(SmallInteger, ForeignKey("warehouses.id"), nullable=False)
    started_by: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    tsd_device_id: Mapped[int | None] = mapped_column(ForeignKey("tsd_devices.id"))
    status: Mapped[str] = mapped_column(String(20), default="ACTIVE")
    notes: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[DateTime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    completed_at: Mapped[DateTime | None] = mapped_column(DateTime(timezone=True))
    order_count: Mapped[int] = mapped_column(default=0)
