import asyncio
import datetime
import logging
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from app.config import settings
from app.services.kaspi_poller import run_poll

logging.basicConfig(level=settings.log_level)
logger = logging.getLogger("wms.scheduler")


async def retry_moysklad_pending():
    """Retry MoySklad demand creation for scanned orders not yet synced."""
    if not settings.moysklad_token:
        return

    from sqlalchemy import select, update, exists as sql_exists
    from app.database import AsyncSessionLocal
    from app.models.kaspi_order import KaspiOrder
    from app.models.scanned_order import ScannedOrder
    from app.services import moysklad_service

    async with AsyncSessionLocal() as db:
        # Orders scanned with SUCCESS but not yet synced to MoySklad
        scanned_subq = sql_exists().where(
            ScannedOrder.order_id == KaspiOrder.id,
            ScannedOrder.scan_result == "SUCCESS",
        )
        orders = (await db.scalars(
            select(KaspiOrder)
            .where(KaspiOrder.moysklad_status == "PENDING")
            .where(scanned_subq)
            .limit(50)
        )).all()

        if not orders:
            return

        logger.info(f"MoySklad retry: {len(orders)} pending orders")
        synced = 0
        for order in orders:
            try:
                ms = await moysklad_service.sync_demand(order.kaspi_order_code)
                if ms["status"] in ("CREATED", "EXISTS"):
                    await db.execute(
                        update(KaspiOrder).where(KaspiOrder.id == order.id).values(
                            moysklad_status="SYNCED",
                            moysklad_demand_id=ms.get("demand_id"),
                            moysklad_synced_at=datetime.datetime.now(datetime.timezone.utc),
                        )
                    )
                    synced += 1
            except Exception as e:
                logger.warning(f"MoySklad retry error for {order.kaspi_order_code}: {e}")

        if synced:
            await db.commit()
            logger.info(f"MoySklad retry: synced {synced}/{len(orders)}")


async def main():
    scheduler = AsyncIOScheduler()
    scheduler.add_job(
        run_poll,
        "interval",
        seconds=settings.kaspi_poll_interval_seconds,
        id="kaspi_poll",
        max_instances=1,
        misfire_grace_time=60,
    )
    if settings.moysklad_token:
        scheduler.add_job(
            retry_moysklad_pending,
            "interval",
            minutes=5,
            id="moysklad_retry",
            max_instances=1,
            misfire_grace_time=60,
        )
    scheduler.start()
    logger.info(f"Scheduler started — Kaspi poll every {settings.kaspi_poll_interval_seconds}s")

    await run_poll()
    if settings.moysklad_token:
        await retry_moysklad_pending()

    try:
        while True:
            await asyncio.sleep(3600)
    except (KeyboardInterrupt, SystemExit):
        scheduler.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
