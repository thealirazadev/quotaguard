"""Background scheduler loops: webhook delivery and rollup job."""

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from app.config import get_settings
from app.services import rollup, webhooks

logger = logging.getLogger("quotaguard.scheduler")


class Scheduler:
    """Manages the webhook delivery and rollup scheduler loops."""

    def __init__(self):
        self.webhook_task: asyncio.Task | None = None
        self.rollup_task: asyncio.Task | None = None

    async def webhook_loop(self) -> None:
        """Periodically deliver due webhooks."""
        settings = get_settings()
        LOOP_INTERVAL = 5  # Check every 5 seconds; actual delivery is gated by next_attempt_at.

        while True:
            try:
                processed = webhooks.deliver_webhooks()
                if processed > 0:
                    logger.debug("webhook delivery pass", extra={"processed": processed})
            except Exception as exc:
                logger.exception("webhook loop error: %s", exc.__class__.__name__)

            await asyncio.sleep(LOOP_INTERVAL)

    async def rollup_loop(self) -> None:
        """Periodically run the rollup job."""
        settings = get_settings()
        interval = settings.rollup_interval_seconds

        if interval == 0:
            logger.info("rollup loop disabled (ROLLUP_INTERVAL_SECONDS=0)")
            return

        while True:
            try:
                stats = rollup.run()
                logger.info(
                    "rollup scheduled",
                    extra={
                        "scanned": stats["scanned"],
                        "upserted": stats["upserted"],
                        "restored": stats["restored"],
                        "reconciled": stats["reconciled"],
                    },
                )
            except rollup.LockNotAcquired:
                logger.debug("rollup.skipped_locked")
            except Exception as exc:
                logger.exception("rollup loop error: %s", exc.__class__.__name__)

            await asyncio.sleep(interval)

    async def start(self) -> None:
        """Start the scheduler loops."""
        logger.info("scheduler starting")
        self.webhook_task = asyncio.create_task(self.webhook_loop())
        self.rollup_task = asyncio.create_task(self.rollup_loop())

    async def stop(self) -> None:
        """Stop the scheduler loops."""
        logger.info("scheduler stopping")
        if self.webhook_task:
            self.webhook_task.cancel()
            try:
                await self.webhook_task
            except asyncio.CancelledError:
                pass
        if self.rollup_task:
            self.rollup_task.cancel()
            try:
                await self.rollup_task
            except asyncio.CancelledError:
                pass


_scheduler: Scheduler | None = None


def get_scheduler() -> Scheduler:
    """Get or create the global scheduler instance."""
    global _scheduler
    if _scheduler is None:
        _scheduler = Scheduler()
    return _scheduler


@asynccontextmanager
async def scheduler_lifespan() -> AsyncIterator[None]:
    """Context manager for starting and stopping the scheduler."""
    scheduler = get_scheduler()
    await scheduler.start()
    try:
        yield
    finally:
        await scheduler.stop()
