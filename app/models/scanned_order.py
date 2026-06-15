from sqlalchemy import String, DateTime, ForeignKey, BigInteger, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func
from app.database import Base


class ScannedOrder(Base):
    __tablename__ = "scanned_orders"
    __table_args__ = (UniqueConstraint("session_id", "order_id"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    session_id: Mapped[int] = mapped_column(ForeignKey("scan_sessions.id", ondelete="CASCADE"), nullable=False)
    order_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("kaspi_orders.id"), nullable=False)
    scanned_by: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    tsd_device_id: Mapped[int | None] = mapped_column(ForeignKey("tsd_devices.id"))
    scan_result: Mapped[str] = mapped_column(String(30), nullable=False)
    lock_holder: Mapped[str | None] = mapped_column(String(200))
    scanned_at: Mapped[DateTime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    released_at: Mapped[DateTime | None] = mapped_column(DateTime(timezone=True))
