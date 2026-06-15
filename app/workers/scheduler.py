import asyncio
import logging
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from app.config import settings
from app.services.kaspi_poller import run_poll

logging.basicConfig(level=settings.log_level)
logger = logging.getLogger("wms.scheduler")


async def refresh_moysklad():
    from app.redis_client import get_redis_pool
    from app.services import moysklad_service
    redis = get_redis_pool()
    try:
        count = await moysklad_service.refresh_cache(redis)
        if count:
            logger.info(f"MoySklad cache: {count} orders loaded")
    except Exception as e:
        logger.error(f"MoySklad cache refresh failed: {e}")


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
            refresh_moysklad,
            "interval",
            minutes=15,
            id="moysklad_cache",
            max_instances=1,
            misfire_grace_time=60,
        )

    scheduler.start()
    logger.info(f"Scheduler started")

    await run_poll()
    if settings.moysklad_token:
        await refresh_moysklad()

    try:
        while True:
            await asyncio.sleep(3600)
    except (KeyboardInterrupt, SystemExit):
        scheduler.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
