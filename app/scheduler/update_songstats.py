"""
Scheduler for updating Songstats data daily
"""

from sqlalchemy.orm import Session
from app.database import SessionLocal
from app.models.models import Songs
from app.logger import get_logger
import asyncio

logger = get_logger("Songstats Updater")


async def update_all_songstats_data():
    """
    Update Songstats data for all tracks in the database.
    This runs once per day to get new datapoints.
    """
    logger.info("Starting daily Songstats data update...")
    db: Session = SessionLocal()

    try:
        # Import here to avoid circular imports
        from app.routers.songstats import fetch_songstats_data_for_song

        # Get all songs with valid ISRCs
        songs = (
            db.query(Songs).filter(Songs.isrc != None, Songs.isrc != "N/A").all()
        )

        if not songs:
            logger.info("No songs found with valid ISRCs")
            return

        logger.info(f"Updating Songstats data for {len(songs)} songs...")

        success_count = 0
        error_count = 0

        for song in songs:
            try:
                result = await fetch_songstats_data_for_song(db, song, fetch_historical=False)
                if result.get("updated"):
                    success_count += 1
                    logger.info(f"✓ Updated {song.title} by {song.artist}")
                else:
                    error_count += 1
                    logger.warning(
                        f"✗ Failed to update {song.title}: {result.get('error', 'Unknown error')}"
                    )
            except Exception as e:
                error_count += 1
                logger.error(f"✗ Error updating {song.title}: {e}")

        logger.info(
            f"Songstats update complete: {success_count} successful, {error_count} errors"
        )

    except Exception as e:
        logger.error(f"Failed to update Songstats data: {e}")
    finally:
        db.close()


def update_songstats_sync():
    """Synchronous wrapper for the async update function"""
    asyncio.run(update_all_songstats_data())
