import asyncio
import logging
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from app.config import settings
from app.services.kaspi_poller import run_poll

logging.basicConfig(level=settings.log_level)
logger = logging.getLogger("wms.scheduler")


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
    scheduler.start()
    logger.info(f"Scheduler started — Kaspi poll every {settings.kaspi_poll_interval_seconds}s")

    # Run immediately on startup
    await run_poll()

    try:
        while True:
            await asyncio.sleep(3600)
    except (KeyboardInterrupt, SystemExit):
        scheduler.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
