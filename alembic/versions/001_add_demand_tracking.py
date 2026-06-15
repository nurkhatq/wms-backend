"""add order_code and demand tracking to scanned_orders

Revision ID: 001
Revises:
Create Date: 2026-06-15
"""
from alembic import op

revision = '001'
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        ALTER TABLE scanned_orders
        ADD COLUMN IF NOT EXISTS order_code VARCHAR(100),
        ADD COLUMN IF NOT EXISTS demand_status VARCHAR(30),
        ADD COLUMN IF NOT EXISTS demand_name VARCHAR(200)
    """)
    op.execute("""
        UPDATE scanned_orders so
        SET order_code = ko.kaspi_order_code
        FROM kaspi_orders ko
        WHERE so.order_id = ko.id AND so.order_code IS NULL
    """)
    op.execute("""
        ALTER TABLE scanned_orders
        ALTER COLUMN order_id DROP NOT NULL
    """)
    op.execute("""
        ALTER TABLE scanned_orders
        DROP CONSTRAINT IF EXISTS scanned_orders_session_id_order_id_key
    """)


def downgrade() -> None:
    pass
