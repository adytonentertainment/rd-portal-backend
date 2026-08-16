import logging
from collections import defaultdict
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Literal

from app.libs.songstats.schemas import HistoricStatsResponse
from app.libs.songstats.songstats_api import SongstatsAPI
from app.models.models import StatsCache, StatsSyncMetadata
from app.schemas.stats import (
    MasterRoyalty,
    Playcount,
    PlayCountHistoryAggregated,
    PublishingRoyalty,
    StatsResponse,
)
from app.settings.settings import get_settings
from sqlalchemy import case, func, inspect
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)
settings = get_settings()
songstats_api = SongstatsAPI()

# Sync settings
SYNC_INTERVAL_HOURS = 6  # Fetch new data every 6 hours
INCREMENTAL_SYNC_DAYS = (
    7  # When doing incremental sync, fetch last 7 days to catch any updates
)

# Songstats data availability settings
# Songstats typically has a 1-day lag (yesterday's data becomes available at some point today)
# Set the hour after which we expect yesterday's data to be available
# Adjust this once you determine when Songstats updates their data
SONGSTATS_DATA_AVAILABLE_HOUR = 15  # 3 PM
SONGSTATS_DATA_LAG_DAYS = (
    1  # Songstats data is delayed by 1 day (yesterday is the latest available)
)

# estimations

# Spotify royalties
# 1000 dollars per million streams
PUBLISHING_ROYALTY_PER_STREAM = 1000 / 1000000

# 3800 dollars per million streams
MASTER_ROYALTY_PER_STREAM = 3800 / 1000000

# YouTube royalties
# 400 dollars per million views for 100% publishing
YOUTUBE_PUBLISHING_ROYALTY_PER_VIEW = 400 / 1000000
# 2000 dollars per million views for 100% master
YOUTUBE_MASTER_ROYALTY_PER_VIEW = 2000 / 1000000


async def get_track_playcounts(
    db: Session,
    track_ids: List[str],
    time_interval: Literal[
        "all_time",
        "last_365_days",
        "year_to_date",
        "last_30_days",
        "last_7_days",
        "today",
    ],
    streaming_services: List[str],
    track_royalties: Dict[
        str, Dict[str, float]
    ] = None,  # Optional: {track_id: {master_royalty, publishing_royalty}}
) -> StatsResponse:
    logger.debug(
        f"[get_track_playcounts] Starting - time_interval={time_interval}, "
        f"track_count={len(track_ids)}, streaming_services={streaming_services}"
    )

    start_date, end_date = _time_interval_to_dates(time_interval)

    # Check and update cache for each track
    for track_id in track_ids:
        sync_metadata = (
            db.query(StatsSyncMetadata)
            .filter(StatsSyncMetadata.spotify_track_id == track_id)
            .first()
        )

        should_sync = False
        sync_start_date = None
        is_full_sync = (
            False  # Track whether this is a full sync (new track) vs incremental
        )

        if not sync_metadata:
            # No data exists - do full sync
            # Explicitly set start_date to 10 years ago to ensure we get ALL historical data
            # Without a start_date, Songstats API may only return recent data
            should_sync = True
            is_full_sync = True
            sync_start_date = date.today() - timedelta(
                days=365 * 10
            )  # 10 years of history
            logger.info(
                f"No sync metadata found for track {track_id} - doing full sync from {sync_start_date}"
            )
        else:
            # Check if existing cached data has sufficient historical coverage
            # If the oldest data point is too recent, we need a full sync to get historical data
            # This handles the case where a track was previously synced with limited data
            oldest_cache_entry = (
                db.query(func.min(StatsCache.date_added))
                .filter(StatsCache.spotify_track_id == track_id)
                .scalar()
            )

            # If oldest cached data is less than 1 year old (or no data at all),
            # we're missing historical data. Force a full sync to get complete history.
            # Exception: if we already did a full sync recently (within 24 hours), skip.
            # This prevents repeated full syncs for legitimately new tracks.
            one_year_ago = date.today() - timedelta(days=365)
            time_since_sync = datetime.now() - sync_metadata.last_synced_at
            hours_since_sync = time_since_sync.total_seconds() / 3600
            recently_synced = hours_since_sync < 24

            needs_full_sync = (
                not recently_synced  # Don't re-sync if synced within 24 hours
                and (oldest_cache_entry is None or oldest_cache_entry > one_year_ago)
            )

            if needs_full_sync:
                # Cached data is missing or less than 1 year old - we're missing historical data
                # Force a full sync to get complete history
                logger.info(
                    f"Track {track_id} has limited historical data (oldest: {oldest_cache_entry}) - forcing full sync"
                )
                should_sync = True
                is_full_sync = True
                sync_start_date = date.today() - timedelta(
                    days=365 * 10
                )  # 10 years of history
            else:
                # Check if we need to do an incremental update
                # (hours_since_sync already calculated above)
                today = date.today()
                now = datetime.now()

                # Determine the "effective" date for syncing
                # Songstats has a lag of SONGSTATS_DATA_LAG_DAYS (typically 1 day)
                # Before SONGSTATS_DATA_AVAILABLE_HOUR, we add an extra day of lag
                # After that hour, we expect the lagged data to be available
                if now.hour < SONGSTATS_DATA_AVAILABLE_HOUR:
                    # Before the update hour, data is lagged by an extra day
                    effective_sync_date = today - timedelta(
                        days=SONGSTATS_DATA_LAG_DAYS + 1
                    )
                else:
                    # After the update hour, expect data up to (today - lag)
                    effective_sync_date = today - timedelta(
                        days=SONGSTATS_DATA_LAG_DAYS
                    )

                # Sync if: enough time has passed OR the synced data is not up to date
                if (
                    hours_since_sync >= SYNC_INTERVAL_HOURS
                    or sync_metadata.last_synced_date < effective_sync_date
                ):
                    # Do incremental sync - fetch last N days to catch updates
                    should_sync = True
                    sync_start_date = date.today() - timedelta(
                        days=INCREMENTAL_SYNC_DAYS
                    )
                    logger.info(
                        f"Track {track_id} last synced {hours_since_sync:.1f} hours ago, data up to {sync_metadata.last_synced_date} - doing incremental sync from {sync_start_date}"
                    )

        if should_sync:
            try:
                # Fetch historical data from Songstats API
                stats_history: HistoricStatsResponse = await songstats_api.get_track_playcount_history(
                    spotify_track_id=track_id,
                    sources=["youtube", "spotify"],
                    start_date=sync_start_date,  # 10 years ago for full sync, recent date for incremental
                )

                # For incremental syncs, don't aggregate - store daily data as-is
                # For full syncs, aggregate old data into weekly bins
                if is_full_sync:
                    # Full sync - aggregate the data using our helper function
                    cache_entries = _aggregate_songstats_history(
                        stats_history=stats_history,
                        track_id=track_id,
                        aggregate_window_days=30,
                    )
                else:
                    # Incremental sync - convert raw data to cache entries without aggregation
                    cache_entries = _convert_songstats_to_cache_entries(
                        stats_history=stats_history, track_id=track_id
                    )

                if cache_entries:
                    # Use UPSERT to prevent duplicate entries (race condition safe)
                    # This replaces the old DELETE + INSERT approach which was causing duplicates
                    # IMPORTANT: Only update values if new value > 0, otherwise keep existing
                    # This prevents overwriting historical cumulative data with 0 when API returns partial data
                    dialect_name = db.bind.dialect.name if db.bind else "sqlite"
                    insert_func = (
                        sqlite_insert if dialect_name == "sqlite" else pg_insert
                    )
                    logger.info(
                        f"Storing {len(cache_entries)} cache entries for track {track_id} using {dialect_name}"
                    )

                    for entry_data in cache_entries:
                        stmt = insert_func(StatsCache).values(**entry_data)
                        # Use MAX for SQLite, GREATEST for PostgreSQL to keep larger value
                        # This prevents overwriting existing data with 0 when API returns partial data
                        if dialect_name == "sqlite":
                            # SQLite uses MAX() function
                            stmt = stmt.on_conflict_do_update(
                                index_elements=["spotify_track_id", "date_added"],
                                set_={
                                    "spotify_playcount": func.max(
                                        StatsCache.spotify_playcount,
                                        stmt.excluded.spotify_playcount,
                                    ),
                                    "youtube_playcount": func.max(
                                        StatsCache.youtube_playcount,
                                        stmt.excluded.youtube_playcount,
                                    ),
                                },
                            )
                        else:
                            # PostgreSQL uses GREATEST() function
                            stmt = stmt.on_conflict_do_update(
                                index_elements=["spotify_track_id", "date_added"],
                                set_={
                                    "spotify_playcount": func.greatest(
                                        StatsCache.spotify_playcount,
                                        stmt.excluded.spotify_playcount,
                                    ),
                                    "youtube_playcount": func.greatest(
                                        StatsCache.youtube_playcount,
                                        stmt.excluded.youtube_playcount,
                                    ),
                                },
                            )
                        db.execute(stmt)

                    # Find the most recent date in the new data
                    most_recent_date = max(
                        entry["date_added"] for entry in cache_entries
                    )

                    # Upsert sync metadata (handles race condition with ON CONFLICT)
                    stmt = (
                        insert_func(StatsSyncMetadata)
                        .values(
                            spotify_track_id=track_id,
                            last_synced_at=datetime.now(),
                            last_synced_date=most_recent_date,
                            created_at=datetime.now(),
                        )
                        .on_conflict_do_update(
                            index_elements=["spotify_track_id"],
                            set_={
                                "last_synced_at": datetime.now(),
                                "last_synced_date": most_recent_date,
                            },
                        )
                    )
                    db.execute(stmt)

                    db.commit()
                    logger.info(
                        f"Cached {len(cache_entries)} entries for track {track_id}, most recent date: {most_recent_date}"
                    )
                    # Log sample entries for debugging duplicate issues
                    if cache_entries and logger.isEnabledFor(logging.DEBUG):
                        sample_entries = (
                            cache_entries[:3] + cache_entries[-3:]
                            if len(cache_entries) > 6
                            else cache_entries
                        )
                        for entry in sample_entries:
                            logger.debug(
                                f"  {entry['date_added']}: Spotify={entry['spotify_playcount']}, YouTube={entry['youtube_playcount']}"
                            )

            except Exception as e:
                logger.error(
                    f"Failed to fetch or process stats history from songstats for spotify track {track_id}: {e}"
                )
                db.rollback()
                continue

    logger.debug(
        f"[get_track_playcounts] Building query - date_range=({start_date} to {end_date}), "
        f"track_ids_count={len(track_ids)}"
    )

    query = db.query(StatsCache).filter(StatsCache.spotify_track_id.in_(track_ids))

    if start_date is not None:
        query = query.filter(StatsCache.date_added >= start_date)
    if end_date is not None:
        query = query.filter(StatsCache.date_added <= end_date)

    cache_entries = query.order_by(StatsCache.date_added).all()

    logger.info(
        f"[get_track_playcounts] Query executed - returned {len(cache_entries)} cache entries "
        f"for date_range=({start_date} to {end_date})"
    )
    # Log date range of cached data for debugging
    if cache_entries:
        dates = [e.date_added for e in cache_entries]
        min_date = min(dates)
        max_date = max(dates)
        logger.info(
            f"[get_track_playcounts] Cache data spans {min_date} to {max_date} "
            f"({len(set(dates))} unique dates)"
        )
        date_track_pairs = [(e.date_added, e.spotify_track_id) for e in cache_entries]
        if len(date_track_pairs) != len(set(date_track_pairs)):
            logger.warning(
                f"[get_track_playcounts] DUPLICATE DATES DETECTED in query results! "
                f"Total entries: {len(date_track_pairs)}, Unique: {len(set(date_track_pairs))}"
            )
    else:
        logger.warning(
            f"[get_track_playcounts] NO CACHE ENTRIES found for time_interval={time_interval}! "
            f"Query was: track_ids={track_ids[:3]}..., date_range=({start_date} to {end_date})"
        )

    # Organize data by service and aggregate by date
    aggregated_data = PlayCountHistoryAggregated()

    # First, organize entries by track and carry forward zero values per track
    # This prevents cliff drops when API returns partial data (e.g., Spotify=0 but YouTube=63M)
    entries_by_track = defaultdict(list)
    for entry in cache_entries:
        entries_by_track[entry.spotify_track_id].append(entry)

    # Collect all unique dates across all tracks
    all_dates = set()
    for entry in cache_entries:
        all_dates.add(entry.date_added)
    sorted_dates = sorted(all_dates)

    # Build a complete data grid: for each track, fill in ALL dates with carried-forward values
    # This fixes the issue where tracks with missing dates cause aggregate totals to drop
    track_date_values = {}  # {track_id: {date: {spotify: val, youtube: val}}}

    for track_id, track_entries in entries_by_track.items():
        # Sort by date
        track_entries.sort(key=lambda x: x.date_added)

        # Build a map of existing values for this track
        track_values_by_date = {}
        for entry in track_entries:
            track_values_by_date[entry.date_added] = {
                'spotify': entry.spotify_playcount or 0,
                'youtube': entry.youtube_playcount or 0
            }

        # Fill ALL dates, carrying forward the last known value
        # Initialize with the track's first known values instead of 0
        # For cumulative data, the count shortly before tracking started
        # is approximately equal to the first tracked value.
        # This prevents misleading cliff spikes in charts when tracks
        # are added to tracking mid-range.
        first_known_spotify = 0
        first_known_youtube = 0
        for entry in track_entries:
            if first_known_spotify == 0 and (entry.spotify_playcount or 0) > 0:
                first_known_spotify = entry.spotify_playcount
            if first_known_youtube == 0 and (entry.youtube_playcount or 0) > 0:
                first_known_youtube = entry.youtube_playcount
            if first_known_spotify > 0 and first_known_youtube > 0:
                break

        prev_spotify = first_known_spotify
        prev_youtube = first_known_youtube
        filled_values = {}

        for d in sorted_dates:
            if d in track_values_by_date:
                # Use actual value, but carry forward if it's 0
                spotify_val = track_values_by_date[d]['spotify']
                youtube_val = track_values_by_date[d]['youtube']

                if spotify_val == 0 and prev_spotify > 0:
                    spotify_val = prev_spotify
                if youtube_val == 0 and prev_youtube > 0:
                    youtube_val = prev_youtube

                prev_spotify = spotify_val if spotify_val > 0 else prev_spotify
                prev_youtube = youtube_val if youtube_val > 0 else prev_youtube
            else:
                # No data for this date - carry forward previous values
                spotify_val = prev_spotify
                youtube_val = prev_youtube

            filled_values[d] = {'spotify': spotify_val, 'youtube': youtube_val}

        track_date_values[track_id] = filled_values

    # Aggregate across all tracks for each date
    if "spotify" in streaming_services:
        spotify_by_date = defaultdict(int)
        for track_id, date_values in track_date_values.items():
            for d, values in date_values.items():
                spotify_by_date[d] += values['spotify']

        # Convert to list of Playcount objects
        spotify_history = [
            Playcount(date=date_val, playcount=playcount, source=None)
            for date_val, playcount in sorted(spotify_by_date.items())
            if playcount > 0
        ]
        if spotify_history:
            aggregated_data.spotify = spotify_history

    if "youtube" in streaming_services:
        youtube_by_date = defaultdict(int)
        for track_id, date_values in track_date_values.items():
            for d, values in date_values.items():
                youtube_by_date[d] += values['youtube']

        # Convert to list of Playcount objects
        youtube_history = [
            Playcount(date=date_val, playcount=playcount, source=None)
            for date_val, playcount in sorted(youtube_by_date.items())
            if playcount > 0
        ]
        if youtube_history:
            aggregated_data.youtube = youtube_history

    # Compute royalties by date using platform-specific rates with per-track equity
    # Use the filled track_date_values to ensure consistency with playcount aggregation
    master_by_date = defaultdict(float)
    publishing_by_date = defaultdict(float)

    for track_id, date_values in track_date_values.items():
        # Get per-track equity if available, otherwise default to 100%
        master_pct = 1.0
        pub_pct = 1.0
        if track_royalties and track_id in track_royalties:
            master_pct = track_royalties[track_id].get("master_royalty", 1.0)
            pub_pct = track_royalties[track_id].get("publishing_royalty", 1.0)

        for date_key, values in date_values.items():
            spotify_pc = values['spotify'] if "spotify" in streaming_services else 0
            youtube_pc = values['youtube'] if "youtube" in streaming_services else 0

            if spotify_pc > 0 or youtube_pc > 0:
                # Calculate royalties with platform-specific rates and per-track equity
                # Spotify: Master $0.0038/stream, Publishing $0.001/stream
                # YouTube: Master $0.002/view, Publishing $0.0004/view
                master_revenue = (spotify_pc * MASTER_ROYALTY_PER_STREAM * master_pct) + (
                    youtube_pc * YOUTUBE_MASTER_ROYALTY_PER_VIEW * master_pct
                )
                pub_revenue = (spotify_pc * PUBLISHING_ROYALTY_PER_STREAM * pub_pct) + (
                    youtube_pc * YOUTUBE_PUBLISHING_ROYALTY_PER_VIEW * pub_pct
                )

                master_by_date[date_key] += master_revenue
                publishing_by_date[date_key] += pub_revenue

    # Build royalty lists (sorted by date)
    master_royalty_list = []
    publishing_royalty_list = []

    for date_val in sorted_dates:
        master_rev = master_by_date.get(date_val, 0)
        pub_rev = publishing_by_date.get(date_val, 0)

        if master_rev > 0 or pub_rev > 0:
            master_royalty_list.append(
                MasterRoyalty(date=date_val, master_royalty=master_rev)
            )
            publishing_royalty_list.append(
                PublishingRoyalty(date=date_val, publishing_royalty=pub_rev)
            )

    aggregated_data.master_royalty = master_royalty_list
    aggregated_data.publishing_royalty = publishing_royalty_list

    logger.info(
        f"[get_track_playcounts] Response aggregation complete for {time_interval} - "
        f"spotify_points={len(aggregated_data.spotify) if aggregated_data.spotify else 0}, "
        f"youtube_points={len(aggregated_data.youtube) if aggregated_data.youtube else 0}, "
        f"master_royalty_points={len(master_royalty_list)}, "
        f"publishing_royalty_points={len(publishing_royalty_list)}"
    )
    # Log sample data points for debugging
    if aggregated_data.spotify and logger.isEnabledFor(logging.DEBUG):
        sample = (
            aggregated_data.spotify[:2] + aggregated_data.spotify[-2:]
            if len(aggregated_data.spotify) > 4
            else aggregated_data.spotify
        )
        logger.debug(
            f"[get_track_playcounts] Sample spotify data: {[(str(p.date), p.playcount) for p in sample]}"
        )

    return StatsResponse(data=aggregated_data)


# helper functions


def _time_interval_to_dates(time_interval: str):
    logger.debug(f"[_time_interval_to_dates] Input: time_interval={time_interval}")
    today = date.today()
    if time_interval == "today":
        start_date = today
        end_date = today
    elif time_interval == "last_7_days":
        start_date = today - timedelta(days=6)
        end_date = today
    elif time_interval == "last_30_days":
        start_date = today - timedelta(days=29)
        end_date = today
    elif time_interval == "year_to_date":
        start_date = date(today.year, 1, 1)
        end_date = today
    elif time_interval == "last_365_days":
        start_date = today - timedelta(days=364)
        end_date = today
    elif time_interval == "all_time":
        start_date = None
        end_date = None
    else:
        raise ValueError(f"Unsupported time_interval: {time_interval}")
    logger.debug(
        f"[_time_interval_to_dates] Computed: start_date={start_date}, end_date={end_date}"
    )
    return start_date, end_date


def _convert_songstats_to_cache_entries(
    stats_history: HistoricStatsResponse, track_id: str
) -> List[Dict[str, Any]]:
    """
    Convert Songstats API response to cache entries without aggregation.
    Used for incremental syncs to maintain daily granularity.
    """
    # Build a map of date -> {spotify_count, youtube_count}
    date_map = {}

    # Process each source in stats
    for history_source in stats_history.stats:
        source_name = history_source.source.lower()
        history_points = history_source.data.history if history_source.data else []

        if source_name == "spotify":
            # Process Spotify history points (SpotifyHistoryPoint)
            for point in history_points:
                entry_date = point.date if isinstance(point.date, date) else point.date

                if entry_date not in date_map:
                    date_map[entry_date] = {"spotify": 0, "youtube": 0}

                # SpotifyHistoryPoint has streams_total
                date_map[entry_date]["spotify"] = point.streams_total

        elif source_name == "youtube":
            # Process YouTube history points (YoutubeHistoryPoint)
            for point in history_points:
                entry_date = point.date if isinstance(point.date, date) else point.date

                if entry_date not in date_map:
                    date_map[entry_date] = {"spotify": 0, "youtube": 0}

                # YoutubeHistoryPoint has video_views_total
                date_map[entry_date]["youtube"] = point.video_views_total

    # Convert to cache entries (all daily, no aggregation)
    result = []
    for entry_date, counts in date_map.items():
        result.append(
            {
                "spotify_track_id": track_id,
                "date_added": entry_date,
                "spotify_playcount": counts["spotify"],
                "youtube_playcount": counts["youtube"],
            }
        )

    return result


def _aggregate_songstats_history(
    stats_history: HistoricStatsResponse, track_id: str, aggregate_window_days: int = 30
) -> List[Dict[str, Any]]:
    aggregate_window_date = date.today() - timedelta(days=aggregate_window_days)

    # Build a map of date -> {spotify_count, youtube_count}
    date_map = {}

    # Process each source in stats
    for history_source in stats_history.stats:
        source_name = history_source.source.lower()
        history_points = history_source.data.history if history_source.data else []

        if source_name == "spotify":
            # Process Spotify history points (SpotifyHistoryPoint)
            for point in history_points:
                entry_date = point.date if isinstance(point.date, date) else point.date

                if entry_date not in date_map:
                    date_map[entry_date] = {"spotify": 0, "youtube": 0}

                # SpotifyHistoryPoint has streams_total
                date_map[entry_date]["spotify"] = point.streams_total

        elif source_name == "youtube":
            # Process YouTube history points (YoutubeHistoryPoint)
            for point in history_points:
                entry_date = point.date if isinstance(point.date, date) else point.date

                if entry_date not in date_map:
                    date_map[entry_date] = {"spotify": 0, "youtube": 0}

                # YoutubeHistoryPoint has video_views_total
                date_map[entry_date]["youtube"] = point.video_views_total

    # Separate into daily and older entries
    daily_entries = []
    older_entries = []

    for entry_date, counts in date_map.items():
        row = {
            "date": entry_date,
            "spotify_playcount": counts["spotify"],
            "youtube_playcount": counts["youtube"],
        }

        if entry_date >= aggregate_window_date:
            daily_entries.append(row)
        else:
            older_entries.append(row)

    # Aggregate older entries into weekly bins (Monday to Sunday)
    weekly_agg = {}
    for entry in older_entries:
        # Get Monday of the week
        week_start = entry["date"] - timedelta(days=entry["date"].weekday())
        if week_start not in weekly_agg:
            weekly_agg[week_start] = {"spotify_playcount": 0, "youtube_playcount": 0}
        # Since Songstats returns cumulative, take the max value for the week
        weekly_agg[week_start]["spotify_playcount"] = max(
            weekly_agg[week_start]["spotify_playcount"], entry["spotify_playcount"]
        )
        weekly_agg[week_start]["youtube_playcount"] = max(
            weekly_agg[week_start]["youtube_playcount"], entry["youtube_playcount"]
        )

    # Build result list
    result = []

    # Add daily entries
    for row in daily_entries:
        result.append(
            {
                "spotify_track_id": track_id,
                "date_added": row["date"],
                "spotify_playcount": row["spotify_playcount"],
                "youtube_playcount": row["youtube_playcount"],
            }
        )

    # Add weekly aggregated entries
    for week_start, agg in weekly_agg.items():
        result.append(
            {
                "spotify_track_id": track_id,
                "date_added": week_start,
                "spotify_playcount": agg["spotify_playcount"],
                "youtube_playcount": agg["youtube_playcount"],
            }
        )

    return result
