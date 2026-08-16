"""
Scheduled Tasks for Notifications
Uses APScheduler to run daily and weekly notification checks.
"""
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
import logging

from app.database import SessionLocal
from app.services.notification_service import run_daily_notifications, run_weekly_notifications

logger = logging.getLogger(__name__)

# Global scheduler instance
scheduler: AsyncIOScheduler = None


def get_scheduler() -> AsyncIOScheduler:
    """Get or create the scheduler instance."""
    global scheduler
    if scheduler is None:
        scheduler = AsyncIOScheduler()
    return scheduler


def run_daily_task():
    """Wrapper for daily notification task."""
    logger.info("[SCHEDULER] Running daily notification checks...")
    db = SessionLocal()
    try:
        run_daily_notifications(db)
        logger.info("[SCHEDULER] Daily notification checks completed.")
    except Exception as e:
        logger.error(f"[SCHEDULER] Daily notification task failed: {e}")
    finally:
        db.close()


def run_weekly_task():
    """Wrapper for weekly notification task."""
    logger.info("[SCHEDULER] Running weekly notification checks...")
    db = SessionLocal()
    try:
        run_weekly_notifications(db)
        logger.info("[SCHEDULER] Weekly notification checks completed.")
    except Exception as e:
        logger.error(f"[SCHEDULER] Weekly notification task failed: {e}")
    finally:
        db.close()


def start_scheduler():
    """Start the background scheduler with notification jobs."""
    global scheduler
    scheduler = get_scheduler()

    if scheduler.running:
        logger.info("[SCHEDULER] Scheduler already running.")
        return

    # Daily task at 6 AM UTC
    scheduler.add_job(
        run_daily_task,
        CronTrigger(hour=6, minute=0),
        id="daily_notifications",
        name="Daily notification checks",
        replace_existing=True,
    )

    # Weekly task on Monday at 8 AM UTC
    scheduler.add_job(
        run_weekly_task,
        CronTrigger(day_of_week="mon", hour=8, minute=0),
        id="weekly_notifications",
        name="Weekly notification checks",
        replace_existing=True,
    )

    scheduler.start()
    logger.info("[SCHEDULER] Background scheduler started with notification jobs.")


def stop_scheduler():
    """Stop the background scheduler."""
    global scheduler
    if scheduler and scheduler.running:
        scheduler.shutdown()
        logger.info("[SCHEDULER] Background scheduler stopped.")


# Optional: Manual trigger endpoints for testing
async def trigger_daily_notifications():
    """Manually trigger daily notifications (for testing)."""
    run_daily_task()
    return {"message": "Daily notifications triggered"}


async def trigger_weekly_notifications():
    """Manually trigger weekly notifications (for testing)."""
    run_weekly_task()
    return {"message": "Weekly notifications triggered"}
