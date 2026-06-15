from sqlalchemy import SmallInteger, String, Boolean, DateTime, ForeignKey, Numeric, BigInteger, Text, JSON
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func
from app.database import Base


class KaspiOrder(Base):
    __tablename__ = "kaspi_orders"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    kaspi_order_code: Mapped[str] = mapped_column(String(50), unique=True, nullable=False)
    kaspi_order_id: Mapped[str | None] = mapped_column(String(100))

    warehouse_id: Mapped[int] = mapped_column(SmallInteger, ForeignKey("warehouses.id"), nullable=False)

    kaspi_status: Mapped[str] = mapped_column(String(50), nullable=False)
    kaspi_state: Mapped[str | None] = mapped_column(String(50))
    delivery_mode: Mapped[str | None] = mapped_column(String(50))
    pickup_point_id: Mapped[str | None] = mapped_column(String(100))
    origin_address_b64: Mapped[str | None] = mapped_column(String(100))

    total_price: Mapped[float | None] = mapped_column(Numeric(12, 2))
    customer_name: Mapped[str | None] = mapped_column(String(200))
    customer_phone: Mapped[str | None] = mapped_column(String(50))
    creation_date: Mapped[DateTime | None] = mapped_column(DateTime(timezone=True))
    planned_delivery_date: Mapped[DateTime | None] = mapped_column(DateTime(timezone=True))
    delivery_slot_from: Mapped[str | None] = mapped_column(String(10))
    delivery_slot_to: Mapped[str | None] = mapped_column(String(10))
    waybill_number: Mapped[str | None] = mapped_column(String(50))
    express: Mapped[bool] = mapped_column(Boolean, default=False)

    products_json: Mapped[dict] = mapped_column(JSON, default=list)

    assembled: Mapped[bool] = mapped_column(Boolean, default=False)
    assembled_at: Mapped[DateTime | None] = mapped_column(DateTime(timezone=True))
    assembled_by_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))

    moysklad_status: Mapped[str] = mapped_column(String(20), default="PENDING")
    moysklad_demand_id: Mapped[str | None] = mapped_column(String(100))
    moysklad_synced_at: Mapped[DateTime | None] = mapped_column(DateTime(timezone=True))

    first_seen_at: Mapped[DateTime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_polled_at: Mapped[DateTime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_status_changed_at: Mapped[DateTime | None] = mapped_column(DateTime(timezone=True))

    is_cancelling: Mapped[bool] = mapped_column(Boolean, default=False)
    cancellation_reason: Mapped[str | None] = mapped_column(String(100))
    cancelling_detected_at: Mapped[DateTime | None] = mapped_column(DateTime(timezone=True))
