"""
Stats Cache Cleanup Utilities

This module provides utilities for cleaning up duplicate entries in the StatsCache table
and maintaining data integrity.

Usage:
    python -m app.utils.stats_cleanup --start-date 2024-12-25 --end-date 2026-01-03
"""

import argparse
import logging
from datetime import date, datetime
from typing import Optional

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.database.session import SessionLocal
from app.models.models import StatsCache

logger = logging.getLogger(__name__)


def deduplicate_stats_cache(
    db: Session,
    date_range_start: Optional[date] = None,
    date_range_end: Optional[date] = None,
    dry_run: bool = False
) -> int:
    """
    Remove duplicate entries from StatsCache, keeping the entry with the highest ID (most recent).

    Args:
        db: SQLAlchemy database session
        date_range_start: Optional start date to limit deduplication
        date_range_end: Optional end date to limit deduplication
        dry_run: If True, only report what would be deleted without actually deleting

    Returns:
        Number of duplicate entries deleted (or would be deleted in dry_run mode)
    """
    # Find all (track_id, date) combinations that have duplicates
    subquery = db.query(
        StatsCache.spotify_track_id,
        StatsCache.date_added,
        func.count(StatsCache.id).label('entry_count'),
        func.max(StatsCache.id).label('keep_id')
    )

    if date_range_start:
        subquery = subquery.filter(StatsCache.date_added >= date_range_start)
    if date_range_end:
        subquery = subquery.filter(StatsCache.date_added <= date_range_end)

    # Group by track and date to find duplicates
    duplicates = subquery.group_by(
        StatsCache.spotify_track_id,
        StatsCache.date_added
    ).having(func.count(StatsCache.id) > 1).all()

    if not duplicates:
        logger.info("No duplicate entries found in StatsCache")
        return 0

    logger.info(f"Found {len(duplicates)} (track, date) combinations with duplicates")

    total_deleted = 0

    for dup in duplicates:
        # Count entries to be deleted (all except the one we keep)
        delete_count = dup.entry_count - 1

        if dry_run:
            logger.info(f"[DRY RUN] Would delete {delete_count} duplicate(s) for "
                       f"track={dup.spotify_track_id}, date={dup.date_added}, keeping id={dup.keep_id}")
        else:
            # Delete all entries except the one we want to keep
            deleted = db.query(StatsCache).filter(
                StatsCache.spotify_track_id == dup.spotify_track_id,
                StatsCache.date_added == dup.date_added,
                StatsCache.id != dup.keep_id
            ).delete(synchronize_session=False)

            logger.debug(f"Deleted {deleted} duplicate(s) for "
                        f"track={dup.spotify_track_id}, date={dup.date_added}")

        total_deleted += delete_count

    if not dry_run:
        db.commit()
        logger.info(f"Successfully deleted {total_deleted} duplicate entries from StatsCache")
    else:
        logger.info(f"[DRY RUN] Would delete {total_deleted} duplicate entries from StatsCache")

    return total_deleted


def get_duplicate_summary(db: Session) -> dict:
    """
    Get a summary of duplicate entries in StatsCache.

    Returns:
        Dictionary with summary statistics
    """
    # Total entries
    total_entries = db.query(func.count(StatsCache.id)).scalar()

    # Unique (track, date) combinations - use subquery for SQLite compatibility
    unique_subquery = db.query(
        StatsCache.spotify_track_id,
        StatsCache.date_added
    ).distinct().subquery()

    unique_combinations = db.query(func.count()).select_from(unique_subquery).scalar()

    # Find tracks with most duplicates
    top_duplicates = db.query(
        StatsCache.spotify_track_id,
        func.count(StatsCache.id).label('total_entries'),
        func.count(func.distinct(StatsCache.date_added)).label('unique_dates')
    ).group_by(
        StatsCache.spotify_track_id
    ).having(
        func.count(StatsCache.id) > func.count(func.distinct(StatsCache.date_added))
    ).order_by(
        func.count(StatsCache.id).desc()
    ).limit(10).all()

    return {
        'total_entries': total_entries,
        'unique_combinations': unique_combinations,
        'duplicate_entries': total_entries - unique_combinations if unique_combinations else 0,
        'top_duplicate_tracks': [
            {
                'track_id': t.spotify_track_id,
                'total_entries': t.total_entries,
                'unique_dates': t.unique_dates,
                'duplicates': t.total_entries - t.unique_dates
            }
            for t in top_duplicates
        ]
    }


def main():
    parser = argparse.ArgumentParser(description='Clean up duplicate entries in StatsCache')
    parser.add_argument('--start-date', type=str, help='Start date (YYYY-MM-DD)')
    parser.add_argument('--end-date', type=str, help='End date (YYYY-MM-DD)')
    parser.add_argument('--dry-run', action='store_true', help='Show what would be deleted without actually deleting')
    parser.add_argument('--summary-only', action='store_true', help='Only show summary, do not delete')
    parser.add_argument('-v', '--verbose', action='store_true', help='Enable verbose logging')

    args = parser.parse_args()

    # Configure logging
    log_level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(
        level=log_level,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )

    # Parse dates
    start_date = datetime.strptime(args.start_date, '%Y-%m-%d').date() if args.start_date else None
    end_date = datetime.strptime(args.end_date, '%Y-%m-%d').date() if args.end_date else None

    # Run cleanup
    db = SessionLocal()
    try:
        # Always show summary
        summary = get_duplicate_summary(db)
        print("\n=== StatsCache Duplicate Summary ===")
        print(f"Total entries: {summary['total_entries']}")
        print(f"Unique (track, date) combinations: {summary['unique_combinations']}")
        print(f"Duplicate entries: {summary['duplicate_entries']}")

        if summary['top_duplicate_tracks']:
            print("\nTop tracks with duplicates:")
            for track in summary['top_duplicate_tracks']:
                print(f"  {track['track_id']}: {track['duplicates']} duplicates "
                      f"({track['total_entries']} total, {track['unique_dates']} unique dates)")

        if args.summary_only:
            return

        print("\n=== Running Deduplication ===")
        if start_date or end_date:
            print(f"Date range: {start_date or 'beginning'} to {end_date or 'end'}")

        deleted = deduplicate_stats_cache(
            db,
            date_range_start=start_date,
            date_range_end=end_date,
            dry_run=args.dry_run
        )

        print(f"\n{'Would delete' if args.dry_run else 'Deleted'} {deleted} duplicate entries")

    finally:
        db.close()


if __name__ == '__main__':
    main()
