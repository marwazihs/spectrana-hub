"""APScheduler wiring for the nightly sweep (PLAN.md §M5).

One AsyncIOScheduler per process. Started in FastAPI lifespan, shut down on
exit. Single job: `sweep_expired` daily at 03:17 UTC (off-peak, off-the-hour
to avoid herd effects with hourly external systems).
"""

from __future__ import annotations

import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from app.db.session import SessionFactory
from app.jobs.sweep import sweep_expired


logger = logging.getLogger("hub.jobs.scheduler")


SWEEP_HOUR_UTC = 3
SWEEP_MINUTE_UTC = 17


async def run_sweep_job() -> dict[str, int]:
    """Job entrypoint — opens its own session so cron firing is self-contained."""
    async with SessionFactory() as session:
        return await sweep_expired(session)


def build_scheduler() -> AsyncIOScheduler:
    scheduler = AsyncIOScheduler(timezone="UTC")
    scheduler.add_job(
        run_sweep_job,
        trigger=CronTrigger(
            hour=SWEEP_HOUR_UTC, minute=SWEEP_MINUTE_UTC, timezone="UTC"
        ),
        id="sweep_expired",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    return scheduler


def start_scheduler() -> AsyncIOScheduler:
    s = build_scheduler()
    s.start()
    logger.info(
        "scheduler started: sweep_expired daily @ %02d:%02d UTC",
        SWEEP_HOUR_UTC,
        SWEEP_MINUTE_UTC,
    )
    return s
