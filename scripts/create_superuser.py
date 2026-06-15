"""Run once on server: python scripts/create_superuser.py"""
import asyncio
from app.database import AsyncSessionLocal, engine, Base
from app.models import *  # noqa
from app.models.warehouse import Warehouse
from app.models.user import User
from app.services.auth_service import hash_password


async def main():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    async with AsyncSessionLocal() as db:
        # Seed warehouses
        for wh_id, code, city, points in [
            (1, "PP1", "Shymkent", ["15142052_PP1"]),
            (2, "PP2", "Almaty",   ["15142052_PP2"]),
            (5, "PP5", "Astana",   ["15142052_PP5"]),
        ]:
            existing = await db.get(Warehouse, wh_id)
            if not existing:
                db.add(Warehouse(id=wh_id, code=code, city=city, kaspi_pickup_point_ids=points))

        await db.flush()

        # Create admin user
        admin = User(
            username="admin",
            password_hash=hash_password("admin123"),
            full_name="Администратор",
            warehouse_id=2,
            role="admin",
        )
        db.add(admin)
        await db.commit()
        print("Admin created: admin / admin123")
        print("Warehouses seeded: PP1 (Shymkent), PP2 (Almaty), PP5 (Astana)")


asyncio.run(main())
