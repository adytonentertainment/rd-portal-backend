import logging
from datetime import date, timedelta
from typing import Literal, Dict, Any, Optional
from collections import defaultdict

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.crud.stats import get_track_playcounts, MASTER_ROYALTY_PER_STREAM, PUBLISHING_ROYALTY_PER_STREAM, YOUTUBE_MASTER_ROYALTY_PER_VIEW, YOUTUBE_PUBLISHING_ROYALTY_PER_VIEW
from app.database.session import get_session
from app.models.models import Songs, User, UserCatalog, StatsCache
from app.schemas.stats import StatsResponse
from app.routers.auth import get_user
from app.services.songstats import SongstatsAPI
from app.settings.settings import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()

stats_router = APIRouter(prefix="/stats", tags=["Stats"])

@stats_router.get(
    "/playcount",
    status_code=status.HTTP_200_OK,
    response_model_exclude_none=True  # Exclude None fields from response
)
async def get_playcounts(
    time_interval: Literal['all_time', 'last_365_days', 'year_to_date', 'last_30_days', 'last_7_days', 'today'] = 'last_7_days',
    track_ids: str = None,
    streaming_services: str = "spotify",
    user: User = Depends(get_user),
    db: Session = Depends(get_session)
) -> StatsResponse:

    logger.info(f"[get_playcounts] Request received - time_interval={time_interval}, "
                f"track_ids={track_ids[:100] + '...' if track_ids and len(track_ids) > 100 else track_ids}, "
                f"streaming_services={streaming_services}, user_id={user.id}")

    # Parse streaming_services (comma-separated string)
    selected_services = None
    if streaming_services:
        # Filter out empty strings after splitting
        selected_services = [s.strip().lower() for s in streaming_services.split(",") if s.strip()]
        if not selected_services:
            selected_services = None

    # Parse track_ids if provided (comma-separated string)
    # Also build track_royalties dict for per-track equity
    track_royalties = {}

    if not track_ids:
        # If no specific track_ids, use all user's tracks from UserCatalog
        user_catalog = db.query(UserCatalog, Songs).join(
            Songs, UserCatalog.song_id == Songs.id
        ).filter(UserCatalog.user_id == user.id).all()

        selected_track_ids = []
        for user_cat, song in user_catalog:
            if song.spotify_track_id:
                selected_track_ids.append(song.spotify_track_id)
                track_royalties[song.spotify_track_id] = {
                    "master_royalty": user_cat.master_royalty or 0,
                    "publishing_royalty": user_cat.publishing_royalty or 0
                }
    else:
        # Parse track_ids string and filter to only user's catalog tracks
        requested_track_ids = set(tid.strip() for tid in track_ids.split(",") if tid.strip())
        # Get user's catalog with royalty info
        user_catalog = db.query(UserCatalog, Songs).join(
            Songs, UserCatalog.song_id == Songs.id
        ).filter(UserCatalog.user_id == user.id).all()

        user_catalog_ids = set()
        for user_cat, song in user_catalog:
            if song.spotify_track_id:
                user_catalog_ids.add(song.spotify_track_id)
                track_royalties[song.spotify_track_id] = {
                    "master_royalty": user_cat.master_royalty or 0,
                    "publishing_royalty": user_cat.publishing_royalty or 0
                }

        # Only keep the ones present in user's catalog
        selected_track_ids = list(requested_track_ids & user_catalog_ids)

    return await get_track_playcounts(
        time_interval=time_interval,
        db=db,
        track_ids=selected_track_ids,
        streaming_services=selected_services,
        track_royalties=track_royalties,
    )


@stats_router.get("/search")
async def search_tracks(query: str, limit: int = 10, user=Depends(get_user)):
    """
    Search for tracks in Songstats by name/artist
    """
    if not user:
        raise HTTPException(status_code=403, detail="Authentication required")

    try:
        api = SongstatsAPI(settings.songstats_api_key)
        results = api.search_track(query, limit)

        if not results:
            return {"message": "No results found", "results": []}

        return results

    except Exception as e:
        logger.error(f"Error searching Songstats: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@stats_router.get("/quarterly-revenue")
async def get_quarterly_expected_revenue(
    client_id: Optional[int] = None,
    user: User = Depends(get_user),
    db: Session = Depends(get_session)
) -> Dict[str, Any]:
    """
    Calculate expected quarterly revenue based on streaming data from the catalog.

    For each quarter, calculates:
    - Stream GROWTH = end_of_quarter_cumulative - end_of_previous_quarter_cumulative
    - Revenue = stream_growth * royalty_rate * user_equity_percentage

    Returns quarterly expected revenue that can be compared against actual statement income.
    """

    # Get all user's catalog tracks with their royalty percentages, optionally filtered by client
    query = db.query(UserCatalog, Songs).join(
        Songs, UserCatalog.song_id == Songs.id
    ).filter(UserCatalog.user_id == user.id)
    if client_id is not None:
        query = query.filter(UserCatalog.client_id == client_id)
    user_catalog = query.all()

    if not user_catalog:
        return {
            "quarters": [],
            "quarterly_data": {},
            "total_expected_revenue": 0
        }

    # Get all spotify_track_ids and their royalty splits
    # Also track the earliest release date to determine graph start
    track_royalties = {}
    earliest_release_date = None
    for user_cat, song in user_catalog:
        if song.spotify_track_id:
            track_royalties[song.spotify_track_id] = {
                "master_royalty": user_cat.master_royalty or 0,
                "publishing_royalty": user_cat.publishing_royalty or 0,
                "title": song.title,
                "artist": song.artist
            }
        # Track earliest release date across all catalog songs
        if song.release_date:
            release_dt = song.release_date.date() if hasattr(song.release_date, 'date') else song.release_date
            if earliest_release_date is None or release_dt < earliest_release_date:
                earliest_release_date = release_dt

    if not track_royalties:
        return {
            "quarters": [],
            "quarterly_data": {},
            "total_expected_revenue": 0
        }

    track_ids = list(track_royalties.keys())

    # Get all stats data for user's tracks
    stats_data = db.query(StatsCache).filter(
        StatsCache.spotify_track_id.in_(track_ids)
    ).order_by(StatsCache.date_added).all()

    # Check which tracks are missing from StatsCache and trigger fetch for them
    cached_track_ids = set(stat.spotify_track_id for stat in stats_data)
    missing_track_ids = [tid for tid in track_ids if tid not in cached_track_ids]

    if missing_track_ids:
        logger.info(f"[quarterly-revenue] {len(missing_track_ids)} tracks missing from StatsCache, triggering fetch...")
        try:
            # Trigger stats fetch for missing tracks (this populates StatsCache)
            await get_track_playcounts(
                db=db,
                track_ids=missing_track_ids,
                time_interval='all_time',
                streaming_services=['spotify', 'youtube'],
                track_royalties={tid: track_royalties[tid] for tid in missing_track_ids}
            )
            # Re-query StatsCache after population
            stats_data = db.query(StatsCache).filter(
                StatsCache.spotify_track_id.in_(track_ids)
            ).order_by(StatsCache.date_added).all()
            logger.info(f"[quarterly-revenue] After fetch, have {len(stats_data)} stats entries")
        except Exception as e:
            logger.warning(f"[quarterly-revenue] Failed to fetch stats for missing tracks: {e}")

    if not stats_data:
        return {
            "quarters": [],
            "quarterly_data": {},
            "total_expected_revenue": 0
        }

    # Group stats by track and quarter, keeping MAX playcount per quarter
    # (there are duplicate entries with 0 values, so we need MAX not last-by-date)
    track_quarterly_max = defaultdict(dict)  # {track_id: {quarter: {spotify: max, youtube: max}}}

    for stat in stats_data:
        stat_date = stat.date_added
        year = stat_date.year
        quarter = (stat_date.month - 1) // 3 + 1
        quarter_key = f"{year}-Q{quarter}"
        track_id = stat.spotify_track_id

        if quarter_key not in track_quarterly_max[track_id]:
            track_quarterly_max[track_id][quarter_key] = {
                "spotify": 0,
                "youtube": 0
            }

        # Keep the MAX playcount for each platform in this quarter
        track_quarterly_max[track_id][quarter_key]["spotify"] = max(
            track_quarterly_max[track_id][quarter_key]["spotify"],
            stat.spotify_playcount or 0
        )
        track_quarterly_max[track_id][quarter_key]["youtube"] = max(
            track_quarterly_max[track_id][quarter_key]["youtube"],
            stat.youtube_playcount or 0
        )

    # Calculate revenue per quarter based on stream GROWTH
    quarterly_revenue = defaultdict(lambda: {
        "expected_master_revenue": 0,
        "expected_publishing_revenue": 0,
        "expected_total_revenue": 0,
        "total_spotify_streams": 0,
        "total_youtube_streams": 0,
        "tracks_with_data": 0
    })

    all_quarters = set()

    for track_id, quarters_data in track_quarterly_max.items():
        royalty_info = track_royalties.get(track_id, {})
        master_pct = royalty_info.get("master_royalty", 0)
        pub_pct = royalty_info.get("publishing_royalty", 0)

        # Sort quarters chronologically
        sorted_quarters = sorted(quarters_data.keys())
        all_quarters.update(sorted_quarters)

        # Track previous quarter's end values to calculate growth
        # Use None to indicate we haven't established a baseline yet
        prev_spotify = None
        prev_youtube = None

        for quarter_key in sorted_quarters:
            end_spotify = quarters_data[quarter_key]["spotify"]
            end_youtube = quarters_data[quarter_key]["youtube"]

            # Skip quarters with no data
            if end_spotify == 0 and end_youtube == 0:
                continue

            # First quarter with data becomes the baseline (no growth calculated)
            # This prevents newly added tracks from showing all cumulative as "growth"
            if prev_spotify is None:
                prev_spotify = end_spotify
                prev_youtube = end_youtube
                continue

            # Calculate stream growth for this quarter
            # Growth = current quarter end - previous quarter end
            spotify_growth = max(0, end_spotify - prev_spotify)
            youtube_growth = max(0, end_youtube - prev_youtube)

            # Calculate revenue based on stream growth and royalty percentages
            spotify_master = spotify_growth * MASTER_ROYALTY_PER_STREAM * master_pct
            spotify_pub = spotify_growth * PUBLISHING_ROYALTY_PER_STREAM * pub_pct
            youtube_master = youtube_growth * YOUTUBE_MASTER_ROYALTY_PER_VIEW * master_pct
            youtube_pub = youtube_growth * YOUTUBE_PUBLISHING_ROYALTY_PER_VIEW * pub_pct

            master_revenue = spotify_master + youtube_master
            pub_revenue = spotify_pub + youtube_pub

            quarterly_revenue[quarter_key]["expected_master_revenue"] += master_revenue
            quarterly_revenue[quarter_key]["expected_publishing_revenue"] += pub_revenue
            quarterly_revenue[quarter_key]["expected_total_revenue"] += master_revenue + pub_revenue
            quarterly_revenue[quarter_key]["total_spotify_streams"] += spotify_growth
            quarterly_revenue[quarter_key]["total_youtube_streams"] += youtube_growth
            quarterly_revenue[quarter_key]["tracks_with_data"] += 1

            # Update previous values for next quarter calculation
            prev_spotify = end_spotify
            prev_youtube = end_youtube

    # Generate all quarters from earliest streaming data to today
    # This ensures the graph shows the full timeline based on actual streaming data
    today = date.today()

    # Use earliest streaming data quarter as the start (not release date)
    if all_quarters:
        earliest_data_quarter = min(all_quarters)
        start_year = int(earliest_data_quarter.split('-Q')[0])
        start_quarter = int(earliest_data_quarter.split('-Q')[1])
    elif earliest_release_date:
        # Fallback to release date if no streaming data yet
        start_year = earliest_release_date.year
        start_quarter = (earliest_release_date.month - 1) // 3 + 1
    else:
        # Last resort: current quarter
        start_year = today.year
        start_quarter = (today.month - 1) // 3 + 1

    end_year = today.year
    end_quarter = (today.month - 1) // 3 + 1

    # Build complete list of quarters from start to end
    complete_quarters = []
    year = start_year
    quarter = start_quarter
    while year < end_year or (year == end_year and quarter <= end_quarter):
        complete_quarters.append(f"{year}-Q{quarter}")
        quarter += 1
        if quarter > 4:
            quarter = 1
            year += 1

    # Build quarterly data with labels matching the frontend format
    # Include all quarters, with 0 values for quarters without data
    quarterly_data = {}
    total_expected = 0

    for quarter_key in complete_quarters:
        if quarter_key in quarterly_revenue:
            data = quarterly_revenue[quarter_key]
            quarterly_data[quarter_key] = {
                "expected_master_revenue": round(data["expected_master_revenue"], 2),
                "expected_publishing_revenue": round(data["expected_publishing_revenue"], 2),
                "expected_total_revenue": round(data["expected_total_revenue"], 2),
                "total_streams": data["total_spotify_streams"] + data["total_youtube_streams"],
                "spotify_streams": data["total_spotify_streams"],
                "youtube_streams": data["total_youtube_streams"],
                "tracks_with_data": data["tracks_with_data"]
            }
            total_expected += data["expected_total_revenue"]
        else:
            # No data for this quarter - include with 0 values
            quarterly_data[quarter_key] = {
                "expected_master_revenue": 0,
                "expected_publishing_revenue": 0,
                "expected_total_revenue": 0,
                "total_streams": 0,
                "spotify_streams": 0,
                "youtube_streams": 0,
                "tracks_with_data": 0
            }

    return {
        "quarters": complete_quarters,
        "quarterly_data": quarterly_data,
        "total_expected_revenue": round(total_expected, 2),
        "earliest_release_date": earliest_release_date.isoformat() if earliest_release_date else None
    }


# helper functions

def _get_dates_from_time_interval(time_interval: str):
    today = date.today()
    if time_interval == 'all_time':
        return None, today
    elif time_interval == 'last_365_days':
        return today - timedelta(days=364), today
    elif time_interval == 'year_to_date':
        return date(today.year, 1, 1), today
    elif time_interval == 'last_30_days':
        return today - timedelta(days=29), today
    elif time_interval == 'last_7_days':
        return today - timedelta(days=6), today
    elif time_interval == 'today':
        return today, today
    else:
        return None, today