"""
Background job for aggregating old daily stats to weekly
Should be run daily via cron job or scheduler
"""

import logging
import sys
from datetime import datetime
from pathlib import Path

# Add parent directory to path for imports
sys.path.append(str(Path(__file__).parent.parent.parent))

from app.database.session import SessionLocal
from app.crud.stats_v2 import aggregate_old_stats_to_weekly, cleanup_very_old_stats

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


def main():
    """
    Main function to run the aggregation job.
    This should be scheduled to run daily (e.g., 2 AM).
    """
    logger.info("=" * 50)
    logger.info("Starting stats aggregation job")
    logger.info(f"Current time: {datetime.now()}")

    db = SessionLocal()

    try:
        # Step 1: Aggregate old daily stats to weekly
        logger.info("Step 1: Aggregating old daily stats to weekly...")
        aggregate_old_stats_to_weekly(db, days_threshold=30)

        # Step 2: Clean up very old stats (optional, run less frequently)
        # Only run on Sundays to avoid too frequent deletions
        if datetime.now().weekday() == 6:  # Sunday
            logger.info("Step 2: Cleaning up stats older than 5 years...")
            cleanup_very_old_stats(db, years_threshold=5)

        logger.info("Stats aggregation job completed successfully")

    except Exception as e:
        logger.error(f"Error in stats aggregation job: {e}", exc_info=True)
        db.rollback()
        sys.exit(1)

    finally:
        db.close()
        logger.info("Database connection closed")
        logger.info("=" * 50)


if __name__ == "__main__":
    main()