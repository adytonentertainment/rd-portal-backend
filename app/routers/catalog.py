import logging
from datetime import datetime
from typing import Any, Dict, List, Optional
from urllib.parse import urljoin

import asyncio
import requests
from app.crud import stats as stats_crud
from app.crud.stats import (
    PUBLISHING_ROYALTY_PER_STREAM,
    YOUTUBE_PUBLISHING_ROYALTY_PER_VIEW,
)
from app.database.session import SessionLocal, get_session
from app.misc import get_spotify_access_token
from app.models.models import (
    Songs,
    StatsCache,
    Subscription,
    SubscriptionTier,
    User,
    UserCatalog,
)
from app.routers.auth import get_user
from app.schemas.catalog import (
    BulkAddRequest,
    BulkAddResponse,
    BulkUpdateRequest,
    BulkUpdateResponse,
    TrackResponse,
    TrackUpdate,
)
from app.schemas.search_result import SearchResult
from app.settings.settings import get_settings
from app.utils.lifecycle_valuation import (
    LifecycleValuationEngine,
    StreamingDataPoint,
)
from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import Response
from sqlalchemy import asc, desc, func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)


settings = get_settings()
catalog_router = APIRouter(prefix="/catalog", tags=["Catalog"])
spotify_access_token = get_spotify_access_token()


def get_catalog_limit_for_tier(
    tier: SubscriptionTier, billing_interval: str = "month"
) -> int:
    """Get the catalog limit based on subscription tier from environment variables.
    For yearly subscriptions, the limit is multiplied by 12."""
    tier_limits = {
        SubscriptionTier.FREE: 0,  # Free tier has no catalog limit from subscription
        SubscriptionTier.ESSENTIAL: settings.essential_catalog_limit,
        SubscriptionTier.PRO: settings.pro_catalog_limit,
        SubscriptionTier.ELITE: settings.elite_catalog_limit,
        SubscriptionTier.ENTERPRISE: settings.enterprise_catalog_limit,
    }
    limit = tier_limits.get(tier, 0)  # Return 0 for unknown tiers
    if billing_interval == "year":
        limit *= 12
    return limit


@catalog_router.post(
    "/tracks", status_code=status.HTTP_201_CREATED, response_model=TrackResponse
)
async def add_to_catalog(
    request: Request,
    items: List[SearchResult],
    db: Session = Depends(get_session),
    user: Optional[User] = Depends(get_user),
    client_id: Optional[int] = None,
):
    # Check user's subscription
    subscription = (
        db.query(Subscription).filter(Subscription.user_id == user.id).first()
    )

    # FREE TIER: Users without subscription can add up to 5 tracks total
    FREE_TIER_CATALOG_LIMIT = 5

    if not subscription:
        # Count how many tracks the user currently has in their catalog
        current_catalog_count = (
            db.query(func.count(UserCatalog.id))
            .filter(UserCatalog.user_id == user.id)
            .scalar()
        )

        # Check if FREE tier user has reached their limit
        if current_catalog_count >= FREE_TIER_CATALOG_LIMIT:
            raise HTTPException(
                status_code=403,
                detail=f"FREE tier limit: You can only have {FREE_TIER_CATALOG_LIMIT} tracks in your catalog. "
                f"Please upgrade to add more tracks.",
            )

        # Calculate remaining quota for FREE tier
        remaining_quota = FREE_TIER_CATALOG_LIMIT - current_catalog_count

        # Check if requested items exceed FREE tier remaining quota
        if len(items) > remaining_quota:
            raise HTTPException(
                status_code=403,
                detail=f"You can only add {remaining_quota} more track(s) with FREE tier. "
                f"You're trying to add {len(items)} track(s). Please upgrade to add more.",
            )

        # Set catalog_limit for FREE tier
        catalog_limit = FREE_TIER_CATALOG_LIMIT
    else:
        # Get the catalog limit for the user's tier (x12 for yearly)
        catalog_limit = get_catalog_limit_for_tier(
            subscription.tier, subscription.billing_interval or "month"
        )

        # Check if user has reached their catalog limit for this billing period
        if subscription.catalog_added_count >= catalog_limit:
            raise HTTPException(
                status_code=403,
                detail=f"You have reached your catalog limit of {catalog_limit} tracks for this billing period. "
                f"Please upgrade your subscription or wait for the next billing cycle.",
            )

        # Calculate how many tracks can still be added
        remaining_quota = catalog_limit - subscription.catalog_added_count

        # Check if requested items exceed remaining quota
        if len(items) > remaining_quota:
            raise HTTPException(
                status_code=403,
                detail=f"You can only add {remaining_quota} more track(s) in this billing period. "
                f"You're trying to add {len(items)} track(s).",
            )

    commited_items = []
    skipped_items = []
    tracks_added_count = 0  # Track how many new items were actually added

    # Get Spotify access token for searching
    global spotify_access_token
    if not spotify_access_token:
        spotify_access_token = get_spotify_access_token()

    try:
        for item in items:
            # If ISRC is missing or invalid, search Spotify to get it
            if not item.isrc or item.isrc == "N/A" or item.isrc.startswith("GENIUS-"):
                print(f"Searching Spotify for ISRC: {item.title} by {item.artist}")
                try:
                    # Search Spotify for this track
                    search_url = "https://api.spotify.com/v1/search"
                    headers = {"Authorization": f"Bearer {spotify_access_token}"}
                    params = {
                        "q": f"{item.title} {item.artist}",
                        "type": "track",
                        "limit": 1,
                    }

                    spotify_response = await asyncio.to_thread(
                        requests.get, search_url, headers=headers, params=params, timeout=10
                    )

                    # Refresh token on 401
                    if spotify_response.status_code == 401:
                        spotify_access_token = get_spotify_access_token()
                        headers = {"Authorization": f"Bearer {spotify_access_token}"}
                        spotify_response = await asyncio.to_thread(
                            requests.get, search_url, headers=headers, params=params, timeout=10
                        )

                    if spotify_response.ok:
                        spotify_data = spotify_response.json()
                        tracks = spotify_data.get("tracks", {}).get("items", [])

                        if tracks:
                            spotify_track = tracks[0]
                            # Get ISRC from Spotify
                            spotify_isrc = spotify_track.get("external_ids", {}).get(
                                "isrc", ""
                            )

                            # Only use if valid
                            if spotify_isrc and not spotify_isrc.startswith("GENIUS-"):
                                item.isrc = spotify_isrc
                                item.spotify_track_id = spotify_track.get("id", "")
                                print(f"✓ Found ISRC: {spotify_isrc} for {item.title}")
                            else:
                                print(f"✗ No valid ISRC found for {item.title}")
                                item.isrc = "N/A"
                        else:
                            print(f"✗ No Spotify results for {item.title}")
                            item.isrc = "N/A"
                except Exception as e:
                    print(f"Error searching Spotify for {item.title}: {e}")
                    item.isrc = "N/A"

            # Skip if no valid ISRC
            if not item.isrc or item.isrc == "N/A":
                skipped_items.append(
                    {
                        "title": item.title,
                        "artist": item.artist,
                        "isrc": item.isrc,
                        "reason": "No valid ISRC",
                    }
                )
                continue

            # Step 1: Check if song exists in global Songs table
            existing_song = (
                db.query(Songs)
                .filter(Songs.spotify_track_id == item.spotify_track_id)
                .first()
            )

            if not existing_song:
                # Try by ISRC as fallback
                existing_song = db.query(Songs).filter(Songs.isrc == item.isrc).first()

            if existing_song:
                print(
                    f"♻️  Reusing existing song: {existing_song.title} (song_id={existing_song.id})"
                )
                song_id = existing_song.id

                try:
                    stats_response = await stats_crud.get_track_playcounts(
                        time_interval="last_30_days",
                        db=db,
                        track_ids=[existing_song.spotify_track_id],
                        streaming_services=["spotify", "apple_music", "youtube"],
                    )
                    # Count total data points from all services
                    playcount_entries = 0
                    if stats_response.data.spotify:
                        playcount_entries += len(stats_response.data.spotify)
                    if stats_response.data.apple_music:
                        playcount_entries += len(stats_response.data.apple_music)
                    if stats_response.data.youtube:
                        playcount_entries += len(stats_response.data.youtube)
                except Exception as e:
                    print(f"⚠️  Error checking historical data: {e}")
                    playcount_entries = 0

                if playcount_entries < 10:  # If less than 10 data points
                    print(
                        f"📊 Song has {playcount_entries} entries, may need data fetch via separate endpoint"
                    )
                else:
                    print(
                        f"📈 Song already has {playcount_entries} historical data points"
                    )
            else:
                # Step 2: Create new Songs entry
                new_song = Songs(
                    spotify_track_id=item.spotify_track_id,
                    isrc=item.isrc,
                    title=item.title,
                    artist=item.artist,
                    album=item.album,
                    album_art=item.album_art,
                    release_date=item.date_added,
                    created_at=datetime.now(),
                )
                try:
                    db.add(new_song)
                    db.commit()
                    db.refresh(new_song)
                    song_id = new_song.id
                    print(f"✅ Created new song: {new_song.title} (song_id={song_id})")
                except IntegrityError:
                    # Handle race condition or sequence mismatch - song was created by another request
                    # or sequence is out of sync after data migration
                    db.rollback()
                    existing_song = (
                        db.query(Songs)
                        .filter(
                            (Songs.spotify_track_id == item.spotify_track_id)
                            | (Songs.isrc == item.isrc)
                        )
                        .first()
                    )
                    if existing_song:
                        song_id = existing_song.id
                        print(
                            f"♻️  Song already exists (caught IntegrityError): {existing_song.title} (song_id={song_id})"
                        )
                    else:
                        # If still not found, skip this item
                        print(
                            f"⚠️  IntegrityError but song not found, skipping: {item.title}"
                        )
                        skipped_items.append(
                            {
                                "title": item.title,
                                "artist": item.artist,
                                "isrc": item.isrc,
                                "reason": "Database error - please try again",
                            }
                        )
                        continue

            # Step 3: Check if user already has this song in their UserCatalog (scoped to client)
            catalog_check = db.query(UserCatalog).filter(
                UserCatalog.user_id == user.id, UserCatalog.song_id == song_id
            )
            if client_id is not None:
                catalog_check = catalog_check.filter(UserCatalog.client_id == client_id)
            else:
                catalog_check = catalog_check.filter(UserCatalog.client_id.is_(None))
            existing_user_catalog = catalog_check.first()

            if existing_user_catalog:
                skipped_items.append(
                    {
                        "title": item.title,
                        "artist": item.artist,
                        "isrc": item.isrc,
                        "reason": "Already in your catalog",
                    }
                )
                continue

            # Step 4: Add to user's UserCatalog with royalty splits
            user_catalog_entry = UserCatalog(
                user_id=user.id,
                song_id=song_id,
                client_id=client_id,
                publishing_royalty=getattr(item, "publishing_royalty", None),
                master_royalty=getattr(item, "master_royalty", None),
                date_added=item.date_added if item.date_added else datetime.now(),
                is_infringement=item.is_infringement if item.is_infringement else False,
            )

            db.add(user_catalog_entry)
            db.commit()
            db.refresh(user_catalog_entry)

            # For response, we'll create a compatible object
            # Get the song with its latest streaming data
            song = db.query(Songs).filter(Songs.id == song_id).first()

            # Fetch the latest playcount data from StatsCache to include in response
            songstats = None
            playcount = 0
            try:
                from datetime import datetime as dt
                from datetime import timedelta

                stats_response = await stats_crud.get_track_playcounts(
                    time_interval="last_7_days",
                    db=db,
                    track_ids=[song.spotify_track_id],
                    streaming_services=["spotify", "apple_music", "youtube"],
                )

                # Get the most recent playcount from each service
                spotify_count = 0
                apple_music_count = 0
                youtube_count = 0
                last_updated = None

                if stats_response.data.spotify and len(stats_response.data.spotify) > 0:
                    latest_spotify = stats_response.data.spotify[
                        -1
                    ]  # Last entry is most recent
                    spotify_count = latest_spotify.playcount
                    last_updated = latest_spotify.date

                if (
                    stats_response.data.apple_music
                    and len(stats_response.data.apple_music) > 0
                ):
                    latest_apple = stats_response.data.apple_music[-1]
                    apple_music_count = latest_apple.playcount
                    if not last_updated or latest_apple.date > last_updated:
                        last_updated = latest_apple.date

                if stats_response.data.youtube and len(stats_response.data.youtube) > 0:
                    latest_youtube = stats_response.data.youtube[-1]
                    youtube_count = latest_youtube.playcount
                    if not last_updated or latest_youtube.date > last_updated:
                        last_updated = latest_youtube.date

                if spotify_count > 0 or apple_music_count > 0 or youtube_count > 0:
                    total_streams_calc = (
                        spotify_count + apple_music_count + youtube_count
                    )
                    playcount = total_streams_calc
                    songstats = {
                        "spotify_streams": spotify_count,
                        "apple_music_streams": apple_music_count,
                        "youtube_streams": youtube_count,
                        "total_streams": total_streams_calc,
                        "last_updated": last_updated.isoformat()
                        if last_updated
                        else None,
                    }
                    print(
                        f"📊 Including songstats in response for {song.title}: total_streams={total_streams_calc}"
                    )
                else:
                    print(f"⚠️  No playcount data found in StatsCache for {song.title}")
            except Exception as e:
                print(f"⚠️  Error fetching playcount for response: {e}")

            # Create a dict that looks like the old Catalog model for backward compatibility
            catalog_like = {
                "id": user_catalog_entry.id,
                "artist": song.artist,
                "title": song.title,
                "album": song.album,
                "isrc": song.isrc,
                "album_art": song.album_art,
                "date_added": user_catalog_entry.date_added,
                "user_id": user.id,
                "spotify_track_id": song.spotify_track_id,
                "publishing_royalty": user_catalog_entry.publishing_royalty,
                "master_royalty": user_catalog_entry.master_royalty,
                "is_infringement": user_catalog_entry.is_infringement,
                "case_status": user_catalog_entry.case_status,
                "duration": 0,
                "playcount": playcount,
                "songstats": songstats,
            }

            commited_items.append(catalog_like)
            tracks_added_count += 1  # Increment counter for successfully added track
            print(f"✅ Added to user catalog: {song.title}")

    except Exception as e:
        db.rollback()
        import traceback

        traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))

    # Update the subscription's catalog_added_count with the number of tracks actually added (if has subscription)
    if tracks_added_count > 0 and subscription:
        subscription.catalog_added_count += tracks_added_count
        db.add(subscription)
        db.commit()
        print(
            f"📊 Updated subscription catalog count: {subscription.catalog_added_count}/{catalog_limit}"
        )

    # Log skipped items for user feedback
    if skipped_items:
        print(f"Skipped {len(skipped_items)} tracks: {skipped_items}")
        # Raise a warning if ALL tracks were duplicates
        if len(commited_items) == 0:
            raise HTTPException(
                status_code=409,
                detail=f"All {len(skipped_items)} tracks were skipped (duplicates or invalid)",
            )

    # Calculate catalog usage for response
    total_catalog_count = (
        db.query(func.count(UserCatalog.id))
        .filter(UserCatalog.user_id == user.id)
        .scalar()
    )

    if subscription:
        catalog_usage = {
            "used": subscription.catalog_added_count,
            "limit": catalog_limit,
            "remaining": catalog_limit - subscription.catalog_added_count,
        }
    else:
        # FREE tier: count total catalog items instead of billing period count
        catalog_usage = {
            "used": total_catalog_count,
            "limit": catalog_limit,
            "remaining": catalog_limit - total_catalog_count,
        }

    return TrackResponse(
        href=str(request.url),
        items=commited_items,
        total=total_catalog_count,
        catalog_usage=catalog_usage,
    )


@catalog_router.post(
    "/bulk-add", status_code=status.HTTP_200_OK, response_model=BulkAddResponse
)
async def bulk_add_to_catalog(
    request: BulkAddRequest,
    client_id: int = None,
    db: Session = Depends(get_session),
    user: User = Depends(get_user),
):
    """
    Bulk add songs from revenue statements to the user's catalog.
    Searches Spotify for each song to get ISRC and spotify_track_id.
    """
    if not request.songs:
        raise HTTPException(status_code=400, detail="No songs provided")

    # Check user's subscription
    subscription = (
        db.query(Subscription).filter(Subscription.user_id == user.id).first()
    )
    FREE_TIER_CATALOG_LIMIT = 5

    if not subscription:
        current_catalog_count = (
            db.query(func.count(UserCatalog.id))
            .filter(UserCatalog.user_id == user.id)
            .scalar()
        )
        remaining_quota = FREE_TIER_CATALOG_LIMIT - current_catalog_count
        catalog_limit = FREE_TIER_CATALOG_LIMIT
    else:
        catalog_limit = get_catalog_limit_for_tier(
            subscription.tier, subscription.billing_interval or "month"
        )
        remaining_quota = catalog_limit - subscription.catalog_added_count

    # Get Spotify access token
    global spotify_access_token
    if not spotify_access_token:
        spotify_access_token = get_spotify_access_token()

    added_count = 0
    skipped_items = []

    for song in request.songs:
        # Check quota before each addition
        if remaining_quota <= 0:
            skipped_items.append(
                {
                    "title": song.title,
                    "artist": song.artist,
                    "reason": "Catalog limit reached",
                }
            )
            continue

        isrc = song.isrc if song.isrc and song.isrc != "N/A" else None
        spotify_track_id = None
        album = ""
        album_art = ""
        spotify_artist = ""
        spotify_title = ""

        try:
            search_url = "https://api.spotify.com/v1/search"
            headers = {"Authorization": f"Bearer {spotify_access_token}"}

            import re
            def normalize_title(t):
                """Normalize title for comparison: lowercase, strip whitespace and punctuation."""
                return re.sub(r'[^a-z0-9\s]', '', t.lower()).strip()

            def normalize_artist(a):
                """Normalize artist name for comparison."""
                return re.sub(r'[^a-z0-9\s]', '', a.lower()).strip()

            def artist_matches(spotify_track, expected_artist):
                """Check if any artist on the Spotify track matches the expected artist."""
                expected = normalize_artist(expected_artist)
                if not expected:
                    return True  # No artist to validate against
                for a in spotify_track.get("artists", []):
                    sp_artist = normalize_artist(a.get("name", ""))
                    if expected in sp_artist or sp_artist in expected:
                        return True
                return False

            # Strategy 1: ISRC lookup (most reliable)
            if isrc:
                params = {"q": f"isrc:{isrc}", "type": "track", "limit": 1}
                resp = requests.get(search_url, headers=headers, params=params)
                if resp.ok:
                    tracks = resp.json().get("tracks", {}).get("items", [])
                    if tracks:
                        spotify_track = tracks[0]
                        sp_title = normalize_title(spotify_track.get("name", ""))
                        expected_title = normalize_title(song.title)
                        # Validate: ISRC result should match song title or artist
                        if sp_title == expected_title or expected_title in sp_title or sp_title in expected_title or artist_matches(spotify_track, song.artist):
                            spotify_track_id = spotify_track.get("id", "")
                            spotify_title = spotify_track.get("name", song.title)
                            artists = spotify_track.get("artists", [])
                            if artists:
                                spotify_artist = artists[0].get("name", "")
                            album_info = spotify_track.get("album", {})
                            album = album_info.get("name", "")
                            images = album_info.get("images", [])
                            if images:
                                album_art = images[0].get("url", "")
                            logger.info(f"Matched by ISRC {isrc}: {spotify_title} by {spotify_artist}")
                        else:
                            sp_artists = ", ".join(a.get("name", "") for a in spotify_track.get("artists", []))
                            logger.warning(f"ISRC {isrc} returned wrong track: '{spotify_track.get('name')}' by {sp_artists} (expected '{song.title}' by {song.artist}) — skipping ISRC match")
                            isrc = None  # Clear bad ISRC so it doesn't pollute the DB

            # Strategy 2: Title + artist search with validation
            if not spotify_track_id:
                query_normalized = normalize_title(song.title)
                # Include artist in search query for better results
                search_query = f'track:"{song.title}" artist:"{song.artist}"'
                params = {"q": search_query, "type": "track", "limit": 10}
                resp = requests.get(search_url, headers=headers, params=params)
                if resp.ok:
                    tracks = resp.json().get("tracks", {}).get("items", [])
                    matched_track = None

                    # First pass: exact title + artist match
                    for t in tracks:
                        result_normalized = normalize_title(t.get("name", ""))
                        if result_normalized == query_normalized and artist_matches(t, song.artist):
                            matched_track = t
                            break

                    # Second pass: exact title only (if artist didn't match — features, aliases)
                    if not matched_track:
                        for t in tracks:
                            result_normalized = normalize_title(t.get("name", ""))
                            if result_normalized == query_normalized:
                                matched_track = t
                                break

                    # Third pass: broader title-only search without artist filter
                    if not matched_track:
                        params2 = {"q": f'track:"{song.title}"', "type": "track", "limit": 10}
                        resp2 = requests.get(search_url, headers=headers, params=params2)
                        if resp2.ok:
                            for t in resp2.json().get("tracks", {}).get("items", []):
                                result_normalized = normalize_title(t.get("name", ""))
                                if result_normalized == query_normalized and artist_matches(t, song.artist):
                                    matched_track = t
                                    break

                    if matched_track:
                        spotify_track_id = matched_track.get("id", "")
                        spotify_title = matched_track.get("name", song.title)
                        artists = matched_track.get("artists", [])
                        if artists:
                            spotify_artist = artists[0].get("name", "")
                        album_info = matched_track.get("album", {})
                        album = album_info.get("name", "")
                        images = album_info.get("images", [])
                        if images:
                            album_art = images[0].get("url", "")
                        # Use ISRC from Spotify result if we didn't have one
                        ext_ids = matched_track.get("external_ids", {})
                        if not isrc and ext_ids.get("isrc"):
                            isrc = ext_ids["isrc"]
                        logger.info(f"Matched by title '{song.title}': {spotify_title} by {spotify_artist}")
                    else:
                        skipped_items.append(
                            {
                                "title": song.title,
                                "artist": song.artist,
                                "reason": "No exact title match found on Spotify",
                            }
                        )
                        logger.info(f"Skipped (no exact title match): {song.title}")
                        continue
                else:
                    logger.warning(f"Spotify title search failed for '{song.title}': {resp.status_code}")
                    skipped_items.append(
                        {
                            "title": song.title,
                            "artist": song.artist,
                            "reason": "Spotify search failed",
                        }
                    )
                    continue

        except Exception as e:
            logger.error(f"Error searching Spotify for {song.title}: {e}")
            skipped_items.append(
                {
                    "title": song.title,
                    "artist": song.artist,
                    "reason": f"Search error: {str(e)}",
                }
            )
            continue

        # Skip if no spotify_track_id found
        if not spotify_track_id:
            skipped_items.append(
                {
                    "title": song.title,
                    "artist": song.artist,
                    "reason": "Could not find on Spotify",
                }
            )
            continue

        # Check if song exists in global Songs table
        existing_song = (
            db.query(Songs).filter(Songs.spotify_track_id == spotify_track_id).first()
        )

        if not existing_song and isrc:
            existing_song = db.query(Songs).filter(Songs.isrc == isrc).first()

        if existing_song:
            song_id = existing_song.id
        else:
            # Create new song - use Spotify data for artist/title (not CSV writer name)
            new_song = Songs(
                spotify_track_id=spotify_track_id,
                isrc=isrc,
                title=spotify_title or song.title,
                artist=spotify_artist
                or song.artist,  # Use Spotify artist, not CSV writer
                album=album,
                album_art=album_art,
                created_at=datetime.now(),
            )
            try:
                db.add(new_song)
                db.commit()
                db.refresh(new_song)
                song_id = new_song.id
            except IntegrityError:
                db.rollback()
                filters = [Songs.spotify_track_id == spotify_track_id]
                if isrc:
                    filters.append(Songs.isrc == isrc)
                from sqlalchemy import or_
                existing_song = (
                    db.query(Songs)
                    .filter(or_(*filters))
                    .first()
                )
                if existing_song:
                    song_id = existing_song.id
                else:
                    skipped_items.append(
                        {
                            "title": song.title,
                            "artist": song.artist,
                            "reason": "Database error",
                        }
                    )
                    continue

        # Check if user already has this song in their catalog (scoped to client)
        catalog_check = db.query(UserCatalog).filter(
            UserCatalog.user_id == user.id, UserCatalog.song_id == song_id
        )
        if client_id is not None:
            catalog_check = catalog_check.filter(UserCatalog.client_id == client_id)
        existing_user_catalog = catalog_check.first()

        if existing_user_catalog:
            skipped_items.append(
                {
                    "title": song.title,
                    "artist": song.artist,
                    "reason": "Already in catalog",
                }
            )
            continue

        # Add to user's catalog
        user_catalog_entry = UserCatalog(
            user_id=user.id,
            song_id=song_id,
            client_id=client_id,
            date_added=datetime.now(),
        )

        db.add(user_catalog_entry)
        db.commit()

        added_count += 1
        remaining_quota -= 1

    # Update subscription catalog count
    if added_count > 0 and subscription:
        subscription.catalog_added_count += added_count
        db.add(subscription)
        db.commit()

    return BulkAddResponse(
        added=added_count,
        skipped=len(skipped_items),
        skipped_items=skipped_items,
        message=f"Successfully added {added_count} songs to catalog. {len(skipped_items)} skipped.",
    )


@catalog_router.post("/resolve-artists", status_code=status.HTTP_200_OK)
async def resolve_catalog_artists(
    db: Session = Depends(get_session),
    user: User = Depends(get_user),
):
    """
    Re-resolve artist names for all songs in user's catalog using Spotify.
    This updates songs that were added with writer names instead of performer names.
    """
    global spotify_access_token
    if not spotify_access_token:
        spotify_access_token = get_spotify_access_token()

    # Get all songs in user's catalog
    user_catalog = (
        db.query(UserCatalog, Songs)
        .join(Songs, UserCatalog.song_id == Songs.id)
        .filter(UserCatalog.user_id == user.id)
        .all()
    )

    updated_count = 0
    failed_items = []

    for user_entry, song in user_catalog:
        try:
            search_url = "https://api.spotify.com/v1/search"
            headers = {"Authorization": f"Bearer {spotify_access_token}"}

            # Search by ISRC first, then by spotify_track_id, then by title
            spotify_artist = None
            spotify_title = None
            album_art = None

            if song.isrc:
                params = {"q": f"isrc:{song.isrc}", "type": "track", "limit": 1}
                response = requests.get(search_url, headers=headers, params=params)

                if response.ok:
                    data = response.json()
                    tracks = data.get("tracks", {}).get("items", [])
                    if tracks:
                        track = tracks[0]
                        artists = track.get("artists", [])
                        if artists:
                            spotify_artist = artists[0].get("name", "")
                        spotify_title = track.get("name", "")
                        images = track.get("album", {}).get("images", [])
                        if images:
                            album_art = images[0].get("url", "")

            # If no result from ISRC, try by title
            if not spotify_artist and song.title:
                params = {"q": song.title, "type": "track", "limit": 1}
                response = requests.get(search_url, headers=headers, params=params)

                if response.ok:
                    data = response.json()
                    tracks = data.get("tracks", {}).get("items", [])
                    if tracks:
                        track = tracks[0]
                        artists = track.get("artists", [])
                        if artists:
                            spotify_artist = artists[0].get("name", "")
                        spotify_title = track.get("name", "")
                        images = track.get("album", {}).get("images", [])
                        if images:
                            album_art = images[0].get("url", "")

            # Update song if we found artist info
            if spotify_artist and spotify_artist != song.artist:
                song.artist = spotify_artist
                if album_art and not song.album_art:
                    song.album_art = album_art
                db.add(song)
                updated_count += 1

        except Exception as e:
            logger.error(f"Error resolving artist for {song.title}: {e}")
            failed_items.append({"title": song.title, "error": str(e)})

    db.commit()

    return {
        "updated": updated_count,
        "failed": len(failed_items),
        "failed_items": failed_items,
        "message": f"Updated {updated_count} songs with resolved artist names.",
    }


@catalog_router.get("/streaming-revenue-analysis")
async def get_streaming_revenue_analysis(
    db: Session = Depends(get_session),
    user: User = Depends(get_user),
) -> Dict[str, Any]:
    """
    Calculate expected streaming revenue for each track in user's catalog
    based on their publishing ownership percentage and stream counts.

    This endpoint returns per-track expected publishing revenue based on:
    - Total stream counts from StatsCache (Spotify + YouTube)
    - User's publishing royalty percentage from UserCatalog
    - Standard publishing royalty rate per stream
    """
    # Get all user's catalog tracks with their royalty percentages
    user_catalog = (
        db.query(UserCatalog, Songs)
        .join(Songs, UserCatalog.song_id == Songs.id)
        .filter(UserCatalog.user_id == user.id)
        .all()
    )

    if not user_catalog:
        return {"tracks": [], "total_expected_revenue": 0}

    tracks_result = []
    total_expected_revenue = 0

    for user_cat, song in user_catalog:
        if not song.spotify_track_id:
            continue

        # Get the latest stats for this track
        latest_stats = (
            db.query(StatsCache)
            .filter(StatsCache.spotify_track_id == song.spotify_track_id)
            .order_by(StatsCache.date_added.desc())
            .first()
        )

        if not latest_stats:
            tracks_result.append(
                {
                    "id": song.id,
                    "title": song.title,
                    "artist": song.artist,
                    "spotify_track_id": song.spotify_track_id,
                    "total_streams": 0,
                    "spotify_streams": 0,
                    "youtube_streams": 0,
                    "publishing_percentage": user_cat.publishing_royalty or 0,
                    "expected_publishing_revenue": 0,
                }
            )
            continue

        # Calculate total streams
        spotify_streams = latest_stats.spotify_playcount or 0
        youtube_streams = latest_stats.youtube_playcount or 0
        total_streams = spotify_streams + youtube_streams

        # Calculate expected publishing revenue with platform-specific rates
        # Spotify: $0.001/stream (Publishing), YouTube: $0.0004/view (Publishing)
        publishing_pct = user_cat.publishing_royalty or 0
        expected_revenue = (
            spotify_streams * PUBLISHING_ROYALTY_PER_STREAM * publishing_pct
        ) + (youtube_streams * YOUTUBE_PUBLISHING_ROYALTY_PER_VIEW * publishing_pct)

        tracks_result.append(
            {
                "id": song.id,
                "title": song.title,
                "artist": song.artist,
                "spotify_track_id": song.spotify_track_id,
                "total_streams": total_streams,
                "spotify_streams": spotify_streams,
                "youtube_streams": youtube_streams,
                "publishing_percentage": publishing_pct,
                "expected_publishing_revenue": round(expected_revenue, 2),
            }
        )

        total_expected_revenue += expected_revenue

    return {
        "tracks": tracks_result,
        "total_expected_revenue": round(total_expected_revenue, 2),
    }


@catalog_router.get("/tracks", response_model=TrackResponse)
async def get_from_catalog(
    request: Request,
    user: User = Depends(get_user),
    db: Session = Depends(get_session),
    artist: str = None,
    title: str = None,
    album: str = None,
    duration: int = None,
    date_added: datetime = None,
    isrc: str = None,
    is_infringement: bool = None,
    album_art: str = None,
    limit: int = None,
    offset: int = 0,
    sort: str = None,
    client_id: Optional[int] = None,
):
    """
    Get tracks from user's catalog using the new UserCatalog + Songs architecture.
    Query joins UserCatalog (user-specific data) with Songs (global song data).
    """
    # Join UserCatalog with Songs
    query = (
        db.query(UserCatalog, Songs)
        .join(Songs, UserCatalog.song_id == Songs.id)
        .filter(UserCatalog.user_id == user.id)
    )

    # Filter by client if specified
    if client_id is not None:
        query = query.filter(UserCatalog.client_id == client_id)

    # Apply filters on Songs fields
    if artist:
        query = query.filter(Songs.artist == artist)
    if title:
        query = query.filter(Songs.title == title)
    if album:
        query = query.filter(Songs.album == album)
    if isrc:
        query = query.filter(Songs.isrc == isrc)
    if album_art:
        query = query.filter(Songs.album_art == album_art)

    # Apply filters on UserCatalog fields
    if date_added:
        query = query.filter(UserCatalog.date_added == date_added)
    if is_infringement is not None:
        query = query.filter(UserCatalog.is_infringement == is_infringement)

    # Handle sorting
    playcount_sort_order = None  # Track if we need playcount sorting
    if sort:
        sort_list = sort.split(",")
        for sort_param in sort_list:
            if not sort_param:
                continue
            order = sort_param[0]
            column_name = sort_param[1:]

            # For playcount sorting, we need to join with StatsCache
            if column_name == "playcount":
                playcount_sort_order = order
                continue
            elif hasattr(Songs, column_name):
                column = getattr(Songs, column_name)
            elif hasattr(UserCatalog, column_name):
                column = getattr(UserCatalog, column_name)
            else:
                continue

            if order == "+":
                query = query.order_by(asc(column))
            elif order == "-":
                query = query.order_by(desc(column))

    # If playcount sorting is requested, join with latest StatsCache
    if playcount_sort_order:
        # Subquery to get the latest date for each track
        latest_stats_subquery = (
            db.query(
                StatsCache.spotify_track_id,
                func.max(StatsCache.date_added).label("max_date"),
            )
            .group_by(StatsCache.spotify_track_id)
            .subquery()
        )

        # Subquery to get the latest playcount for each track
        latest_playcount_subquery = (
            db.query(StatsCache.spotify_track_id, StatsCache.spotify_playcount)
            .join(
                latest_stats_subquery,
                (
                    StatsCache.spotify_track_id
                    == latest_stats_subquery.c.spotify_track_id
                )
                & (StatsCache.date_added == latest_stats_subquery.c.max_date),
            )
            .subquery()
        )

        # Left join with the playcount subquery
        query = query.outerjoin(
            latest_playcount_subquery,
            Songs.spotify_track_id == latest_playcount_subquery.c.spotify_track_id,
        )

        # Apply playcount sorting (NULL values sorted last)
        if playcount_sort_order == "+":
            query = query.order_by(
                func.coalesce(latest_playcount_subquery.c.spotify_playcount, 0).asc()
            )
        elif playcount_sort_order == "-":
            query = query.order_by(
                func.coalesce(latest_playcount_subquery.c.spotify_playcount, 0).desc()
            )

    query = query.offset(offset)
    if limit:
        query = query.limit(limit)

    results = query.all()

    # Collect all spotify_track_ids for batch fetch playcount
    spotify_track_ids = []
    for user_catalog, song in results:
        if song.spotify_track_id:
            spotify_track_ids.append(song.spotify_track_id)

    # Batch fetch latest playcounts from StatsCache for all tracks
    track_playcounts = {}
    if spotify_track_ids:
        # Subquery to get the latest date for each track
        latest_stats_subquery = (
            db.query(
                StatsCache.spotify_track_id,
                func.max(StatsCache.date_added).label("max_date"),
            )
            .filter(StatsCache.spotify_track_id.in_(spotify_track_ids))
            .group_by(StatsCache.spotify_track_id)
            .subquery()
        )

        # Get the actual stats for each track at their latest date
        latest_stats = (
            db.query(StatsCache)
            .join(
                latest_stats_subquery,
                (
                    StatsCache.spotify_track_id
                    == latest_stats_subquery.c.spotify_track_id
                )
                & (StatsCache.date_added == latest_stats_subquery.c.max_date),
            )
            .all()
        )

        for stats in latest_stats:
            track_playcounts[stats.spotify_track_id] = {
                "spotify_playcount": stats.spotify_playcount or 0,
                "youtube_playcount": stats.youtube_playcount or 0,
            }

    # Construct response items with per-track playcounts
    items_parsed = []
    for user_catalog, song in results:
        # Get per-track playcounts
        playcounts = track_playcounts.get(song.spotify_track_id, {})
        spotify_pc = playcounts.get("spotify_playcount", 0)
        youtube_pc = playcounts.get("youtube_playcount", 0)

        item_dict = {
            "id": user_catalog.id,
            "artist": song.artist,
            "title": song.title,
            "album": song.album,
            "duration": 0,
            "user_id": user.id,
            "date_added": user_catalog.date_added,
            "isrc": song.isrc,
            "is_infringement": user_catalog.is_infringement
            if user_catalog.is_infringement is not None
            else False,
            "case_status": user_catalog.case_status,
            "pro": user_catalog.pro,
            "publisher_type": user_catalog.publisher_type,
            "album_art": song.album_art,
            "spotify_track_id": song.spotify_track_id,
            "publishing_royalty": user_catalog.publishing_royalty,
            "master_royalty": user_catalog.master_royalty,
            "spotify_playcount": spotify_pc,
            "youtube_playcount": youtube_pc,
            "playcount": spotify_pc + youtube_pc,
        }
        items_parsed.append(item_dict)

    total_query = db.query(func.count(UserCatalog.id)).filter(
        UserCatalog.user_id == user.id
    )
    if client_id is not None:
        total_query = total_query.filter(UserCatalog.client_id == client_id)
    total = total_query.scalar()

    return {
        "href": str(request.url),
        "items": items_parsed,
        "total": total,
    }


@catalog_router.get("/tracks/{id}", response_model=TrackResponse)
async def get_id_from_catalog(
    request: Request,
    id: int,
    user: User = Depends(get_user),
    db: Session = Depends(get_session),
):
    """Get a specific track from user's catalog by UserCatalog ID"""
    # Query UserCatalog and join with Songs
    query = (
        db.query(UserCatalog, Songs)
        .join(Songs, UserCatalog.song_id == Songs.id)
        .filter(UserCatalog.user_id == user.id, UserCatalog.id == id)
    )

    results = query.all()

    # Collect track IDs for batch playcount fetch
    spotify_track_ids = [
        song.spotify_track_id for _, song in results if song.spotify_track_id
    ]

    # Use crud/stats.py to fetch latest playcount
    latest_playcounts = {}
    if spotify_track_ids:
        from datetime import timedelta, timezone

        today = datetime.now(timezone.utc).date()
        window_start = today - timedelta(
            days=7
        )  # Fetch last 7 days to get latest entry

        try:
            # Use the efficient catalog helper that reads from StatsCache directly
            latest_playcounts = await stats_crud.get_latest_playcounts_for_catalog(
                track_ids=spotify_track_ids, user=user, db=db, days_back=7
            )
        except Exception as e:
            print(f"⚠️  Error fetching playcount data: {e}")

    items_parsed = []
    for user_catalog, song in results:
        # Get latest playcount from stats_crud results
        track_stats = latest_playcounts.get(song.spotify_track_id)
        playcount = track_stats.get("total_playcount", 0) if track_stats else 0

        items_parsed.append(
            {
                "id": user_catalog.id,
                "artist": song.artist,
                "title": song.title,
                "album": song.album,
                "duration": 0,
                "user_id": user.id,
                "date_added": user_catalog.date_added,
                "isrc": song.isrc,
                "is_infringement": user_catalog.is_infringement
                if user_catalog.is_infringement is not None
                else False,
                "case_status": user_catalog.case_status,
                "pro": user_catalog.pro,
                "publisher_type": user_catalog.publisher_type,
                "album_art": song.album_art,
                "spotify_track_id": song.spotify_track_id,
                "publishing_royalty": user_catalog.publishing_royalty,
                "master_royalty": user_catalog.master_royalty,
                "playcount": playcount,
            }
        )

    return {"href": str(request.url), "items": items_parsed, "total": len(items_parsed)}


@catalog_router.patch("/tracks/bulk-update", response_model=BulkUpdateResponse)
async def bulk_update_tracks(
    request_body: BulkUpdateRequest,
    db: Session = Depends(get_session),
    user: User = Depends(get_user),
):
    """Bulk update fields on multiple tracks at once"""
    update_data = request_body.updates.model_dump(exclude_unset=True)

    allowed_fields = {
        "publishing_royalty",
        "master_royalty",
        "is_infringement",
        "case_status",
        "pro",
        "publisher_type",
    }
    filtered_updates = {k: v for k, v in update_data.items() if k in allowed_fields}

    if not filtered_updates:
        raise HTTPException(status_code=400, detail="No valid fields to update")

    updated_count = 0
    failed_ids = []

    for track_id in request_body.track_ids:
        try:
            # Try numeric ID first, then spotify_track_id
            try:
                numeric_id = int(track_id)
                entry = (
                    db.query(UserCatalog)
                    .filter(UserCatalog.id == numeric_id, UserCatalog.user_id == user.id)
                    .first()
                )
            except ValueError:
                entry = (
                    db.query(UserCatalog)
                    .join(Songs, UserCatalog.song_id == Songs.id)
                    .filter(Songs.spotify_track_id == track_id, UserCatalog.user_id == user.id)
                    .first()
                )

            if entry:
                for key, value in filtered_updates.items():
                    setattr(entry, key, value)
                updated_count += 1
            else:
                failed_ids.append(track_id)
        except Exception:
            failed_ids.append(track_id)

    db.commit()

    return BulkUpdateResponse(
        updated_count=updated_count,
        failed_ids=failed_ids,
        message=f"Updated {updated_count} tracks"
        + (f", {len(failed_ids)} failed" if failed_ids else ""),
    )


@catalog_router.patch("/tracks/{id}", response_model=TrackResponse)
async def update_catalog_from_id(
    id: str,
    updated_item: TrackUpdate,
    db: Session = Depends(get_session),
    user: User = Depends(get_user),
):
    print(f"🔧 PATCH /catalog/tracks/{id} called by user {user.id}")
    print(f"🔧 Update data: {updated_item.model_dump(exclude_unset=True)}")

    # Try to convert id to int for UserCatalog ID, if it fails, treat it as spotify_track_id
    try:
        numeric_id = int(id)
        print(f"🔧 Querying UserCatalog with ID={numeric_id}, user_id={user.id}")
        # Query UserCatalog by UserCatalog ID (NEW ARCHITECTURE)
        user_catalog_entry = (
            db.query(UserCatalog)
            .filter(UserCatalog.id == numeric_id, UserCatalog.user_id == user.id)
            .first()
        )
        print(f"🔧 Found entry: {user_catalog_entry}")
    except ValueError:
        # If id is not numeric, search by spotify_track_id via Songs
        user_catalog_entry = (
            db.query(UserCatalog)
            .join(Songs, UserCatalog.song_id == Songs.id)
            .filter(Songs.spotify_track_id == id, UserCatalog.user_id == user.id)
            .first()
        )

    if not user_catalog_entry:
        raise HTTPException(status_code=404, detail="Item not found")

    # Only allow updating user-specific fields (royalty splits)
    update_data = updated_item.model_dump(exclude_unset=True)

    # Filter to only UserCatalog fields (not Songs fields)
    allowed_fields = {
        "publishing_royalty",
        "master_royalty",
        "is_infringement",
        "case_status",
        "pro",
        "publisher_type",
    }
    for key, value in update_data.items():
        if key in allowed_fields:
            setattr(user_catalog_entry, key, value)

    db.commit()
    db.refresh(user_catalog_entry)

    # Get the associated song for the response
    song = db.query(Songs).filter(Songs.id == user_catalog_entry.song_id).first()

    # Create a combined entry object for backward compatibility with response
    class CombinedEntry:
        def __init__(self, user_catalog, song):
            self.id = user_catalog.id
            self.spotify_track_id = song.spotify_track_id
            self.title = song.title
            self.artist = song.artist
            self.album = song.album
            self.isrc = song.isrc
            self.album_art = song.album_art
            self.date_added = user_catalog.date_added
            self.publishing_royalty = user_catalog.publishing_royalty
            self.master_royalty = user_catalog.master_royalty
            self.is_infringement = user_catalog.is_infringement
            self.case_status = user_catalog.case_status
            self.pro = user_catalog.pro
            self.publisher_type = user_catalog.publisher_type
            self.user_id = user_catalog.user_id
            self.duration = 0  # Placeholder for backward compatibility
            self.playcount = None
            self.songstats = None

    entry = CombinedEntry(user_catalog_entry, song)
    print(f"✅ Successfully updated royalty splits for track ID {id}")
    # no use for duration, maybe delete later
    entry.duration = 0
    return {
        "href": urljoin(settings.base_url_backend, f"/catalog/{id}"),
        "items": [entry],
        "total": 1,
    }


@catalog_router.delete("/tracks", status_code=status.HTTP_204_NO_CONTENT)
async def delete_catalog(
    db: Session = Depends(get_session),
    user: User = Depends(get_user),
    client_id: Optional[int] = None,
):
    """Delete all tracks from user's catalog (UserCatalog entries only, not global Songs)"""
    query = db.query(UserCatalog).filter(UserCatalog.user_id == user.id)
    if client_id is not None:
        query = query.filter(UserCatalog.client_id == client_id)
    deleted_count = query.delete(synchronize_session=False)
    db.commit()

    return Response(status_code=status.HTTP_204_NO_CONTENT)


@catalog_router.delete("/tracks/{id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_catalog_from_id(
    id: int, db: Session = Depends(get_session), user: User = Depends(get_user)
):
    """Delete a single track from user's catalog by UserCatalog ID"""
    deleted_count = (
        db.query(UserCatalog)
        .filter(UserCatalog.id == id, UserCatalog.user_id == user.id)
        .delete(synchronize_session=False)
    )
    db.commit()

    if deleted_count == 0:
        raise HTTPException(status_code=404, detail="Track not found in your catalog")

    return Response(status_code=status.HTTP_204_NO_CONTENT)


@catalog_router.get("/valuation", status_code=status.HTTP_200_OK)
async def get_catalog_valuation(
    track_ids: Optional[str] = None,
    client_id: Optional[int] = None,
    db: Session = Depends(get_session),
    user: User = Depends(get_user),
):
    """
    Calculate comprehensive catalog valuation using industry-standard methodologies:
    1. Purchase Multiple Method (6-7x annual revenue based on catalog characteristics)
    2. DCF Analysis (10-year cash flow projections)
    3. Weighted Average (60% PM + 40% DCF)

    Returns complete valuation breakdown with range estimates.
    """
    # Get user's catalog
    query = db.query(UserCatalog).filter(UserCatalog.user_id == user.id)
    if client_id is not None:
        query = query.filter(UserCatalog.client_id == client_id)
    user_catalog = query.all()

    # Filter by specific track IDs if provided
    if track_ids:
        selected_track_ids = [tid.strip() for tid in track_ids.split(",")]
        user_catalog = [
            entry
            for entry in user_catalog
            if entry.song.spotify_track_id in selected_track_ids
        ]

    if not user_catalog:
        return {
            "estimated_value": 0,
            "annual_revenue": 0,
            "track_count": 0,
            "valuation_range": {"low": 0, "mid": 0, "high": 0},
            "purchase_multiple": 0,
            "last_updated": None,
        }

    # UI rates (different for master vs publishing)
    master_rate = 3.8 / 1000  # $0.0038 per stream
    pub_rate = 1.0 / 1000  # $0.001 per stream

    # Initialize valuation engine
    engine = LifecycleValuationEngine(master_rate=master_rate, pub_rate=pub_rate)

    # Calculate revenue using lifecycle-aware analysis
    total_pub_revenue = 0
    total_master_revenue = 0
    last_updated = None
    track_count = len(user_catalog)
    total_pub_ownership = 0
    total_master_ownership = 0
    oldest_release = None
    total_streams = 0

    # Track lifecycle insights for enhanced response
    lifecycle_breakdown = {
        "VIRAL_NEW": 0,
        "BUZZING": 0,
        "MATURING": 0,
        "EVERGREEN": 0,
        "DECLINING": 0,
        "CATALOG": 0,
    }
    track_valuations = []  # Store individual track valuations

    for catalog_entry in user_catalog:
        track_id = catalog_entry.song.spotify_track_id
        pub_royalty = catalog_entry.publishing_royalty or 0
        master_royalty = catalog_entry.master_royalty or 0
        total_pub_ownership += pub_royalty
        total_master_ownership += master_royalty

        # Track oldest release for catalog age
        if catalog_entry.song.release_date:
            if not oldest_release or catalog_entry.song.release_date < oldest_release:
                oldest_release = catalog_entry.song.release_date

        try:
            stats_response = await stats_crud.get_track_playcounts(
                time_interval="all_time",
                db=db,
                track_ids=[track_id],
                streaming_services=["spotify", "youtube"],
            )

            # Convert StatsResponse to list of stats-like objects
            # stats_response.data contains PlayCountHistoryAggregated with spotify/youtube arrays
            all_stats = []

            # Merge spotify and youtube data by date
            from collections import defaultdict

            date_map = defaultdict(lambda: {"spotify": 0, "youtube": 0})

            # Add Spotify playcounts
            if stats_response.data.spotify:
                for entry in stats_response.data.spotify:
                    date_key = (
                        entry.date.isoformat()
                        if hasattr(entry.date, "isoformat")
                        else str(entry.date)
                    )
                    date_map[date_key]["spotify"] = entry.playcount

            # Add YouTube playcounts
            if stats_response.data.youtube:
                for entry in stats_response.data.youtube:
                    date_key = (
                        entry.date.isoformat()
                        if hasattr(entry.date, "isoformat")
                        else str(entry.date)
                    )
                    date_map[date_key]["youtube"] = entry.playcount

            # Convert date_map to list of StatEntry objects
            class StatEntry:
                def __init__(self, date_added, spotify, youtube):
                    self.date_added = date_added
                    self.spotify_playcount = spotify
                    self.youtube_playcount = youtube

            for date_key, counts in date_map.items():
                all_stats.append(
                    StatEntry(date_key, counts["spotify"], counts["youtube"])
                )

            # Sort by date (date_added is an ISO string, need to parse)
            from datetime import datetime as dt

            all_stats.sort(
                key=lambda x: (
                    dt.fromisoformat(x.date_added)
                    if isinstance(x.date_added, str)
                    else x.date_added
                )
            )

            # Forward-fill cumulative values: if Spotify/YouTube has no data for a date,
            # carry forward the last known value (since these are cumulative totals)
            last_spotify = 0
            last_youtube = 0
            for stat in all_stats:
                if stat.spotify_playcount == 0 and last_spotify > 0:
                    stat.spotify_playcount = last_spotify
                else:
                    last_spotify = stat.spotify_playcount

                if stat.youtube_playcount == 0 and last_youtube > 0:
                    stat.youtube_playcount = last_youtube
                else:
                    last_youtube = stat.youtube_playcount

            # Filter out entries where both are still zero (no data at all for that date)
            all_stats = [
                s
                for s in all_stats
                if (s.spotify_playcount > 0 or s.youtube_playcount > 0)
            ]

        except Exception as e:
            logger.error(f"Error fetching historical data for {track_id}: {e}")
            all_stats = []

        if not all_stats or len(all_stats) < 2:
            continue  # Need at least 2 data points for lifecycle analysis

        # Additional filter: Remove leading zeros (backfilled data before actual release)
        # Find the first non-zero data point
        first_nonzero_idx = 0
        for i, stat in enumerate(all_stats):
            if (stat.spotify_playcount or 0) + (stat.youtube_playcount or 0) > 0:
                first_nonzero_idx = i
                break

        # Trim the data to start from first non-zero point
        all_stats = all_stats[first_nonzero_idx:]

        if len(all_stats) < 2:
            continue  # Need at least 2 data points after filtering zeros

        # Filter outliers: Remove data points that show >50x growth from previous point
        # This catches data corruption where tracks get combined or wrong data is recorded
        filtered_stats = [all_stats[0]]  # Always keep first point
        for i in range(1, len(all_stats)):
            prev_plays = (all_stats[i - 1].spotify_playcount or 0) + (
                all_stats[i - 1].youtube_playcount or 0
            )
            curr_plays = (all_stats[i].spotify_playcount or 0) + (
                all_stats[i].youtube_playcount or 0
            )

            # Allow growth, but flag absurd spikes (>50x in one day) as data corruption
            if prev_plays > 0 and curr_plays > prev_plays * 50:
                logger.warning(
                    f"Outlier detected for {catalog_entry.song.title}: {prev_plays:,} -> {curr_plays:,} plays (skipping)"
                )
                continue  # Skip this corrupted data point

            filtered_stats.append(all_stats[i])

        all_stats = filtered_stats

        if len(all_stats) < 2:
            continue  # Need at least 2 data points after filtering outliers

        # Update last_updated tracker
        latest_stat = all_stats[-1]
        # date_added is now an ISO string, can be compared directly
        if not last_updated or latest_stat.date_added > last_updated:
            last_updated = latest_stat.date_added

        # Calculate total streams (latest count)
        current_plays = (latest_stat.spotify_playcount or 0) + (
            latest_stat.youtube_playcount or 0
        )
        total_streams += current_plays

        # Convert StatsCache to StreamingDataPoint format
        # date_added is now an ISO string, need to parse it
        from datetime import datetime as dt

        historical_data = [
            StreamingDataPoint(
                date=dt.fromisoformat(stat.date_added)
                if isinstance(stat.date_added, str)
                else stat.date_added,
                spotify_plays=stat.spotify_playcount or 0,
                youtube_plays=stat.youtube_playcount or 0,
            )
            for stat in all_stats
        ]

        # Get release date (use a fallback if not available)
        # Parse the first date_added if it's a string
        first_date = all_stats[0].date_added
        if isinstance(first_date, str):
            first_date = dt.fromisoformat(first_date)
        release_date = catalog_entry.song.release_date or first_date

        # Calculate lifecycle-aware valuation for this song
        try:
            # Calculate combined valuation (for total and lifecycle insights)
            valuation_result = engine.calculate_song_valuation(
                historical_data=historical_data,
                release_date=release_date,
                master_royalty=master_royalty,
                pub_royalty=pub_royalty,
            )

            # Calculate separate lifecycle valuations for publishing and master
            pub_valuation = None
            master_valuation = None

            if pub_royalty > 0:
                pub_valuation = engine.calculate_song_valuation(
                    historical_data=historical_data,
                    release_date=release_date,
                    master_royalty=0,
                    pub_royalty=pub_royalty,
                )
                total_pub_revenue += pub_valuation.base_revenue

            if master_royalty > 0:
                master_valuation = engine.calculate_song_valuation(
                    historical_data=historical_data,
                    release_date=release_date,
                    master_royalty=master_royalty,
                    pub_royalty=0,
                )
                total_master_revenue += master_valuation.base_revenue

            # Track lifecycle distribution
            lifecycle_breakdown[valuation_result.lifecycle.value] += 1

            # Store individual track valuation for detailed response
            track_valuations.append(
                {
                    "track_id": track_id,
                    "title": catalog_entry.song.title,
                    "artist": catalog_entry.song.artist,
                    "lifecycle": valuation_result.lifecycle.value,
                    "base_revenue": valuation_result.base_revenue,
                    "multiple": valuation_result.multiple,
                    "value": valuation_result.catalog_value,
                    "confidence": valuation_result.confidence,
                    "explanation": valuation_result.explanation,
                    "publishing_value": pub_valuation.catalog_value
                    if pub_valuation
                    else 0,
                    "master_value": master_valuation.catalog_value
                    if master_valuation
                    else 0,
                }
            )

        except Exception as e:
            logger.error(
                f"Error calculating lifecycle valuation for {catalog_entry.song.title}: {e}"
            )
            continue

    annual_pub_revenue = total_pub_revenue
    annual_master_revenue = total_master_revenue
    annual_revenue = annual_pub_revenue + annual_master_revenue

    # Calculate catalog characteristics for valuation
    catalog_age = 0
    if oldest_release:
        catalog_age = (datetime.now().date() - oldest_release.date()).days / 365.25

    avg_pub_ownership = total_pub_ownership / track_count if track_count > 0 else 0
    avg_master_ownership = (
        total_master_ownership / track_count if track_count > 0 else 0
    )
    avg_ownership = (
        (total_pub_ownership + total_master_ownership) / track_count
        if track_count > 0
        else 0
    )

    # Helper function to calculate valuation for a revenue stream
    def calculate_valuation(revenue, ownership_pct):
        # Calculate base multiple based on catalog characteristics
        base_multiple = 5.0  # Start with industry average

        # Adjust for catalog age (older = more stable)
        if catalog_age > 10:
            base_multiple += 2.0
        elif catalog_age > 5:
            base_multiple += 1.0
        elif catalog_age < 2:
            base_multiple -= 1.0

        # Adjust for revenue stability (assume 7/10 for streaming)
        base_multiple += (7 - 5) * 0.3

        # Adjust for genre (latin)
        base_multiple *= 1.1

        # Adjust for rights ownership
        if ownership_pct > 0.75:
            base_multiple += 1.0
        elif ownership_pct < 0.25:
            base_multiple -= 0.5

        # Adjust for catalog size
        if track_count > 100:
            base_multiple += 1.0
        elif track_count > 50:
            base_multiple += 0.5
        elif track_count < 10:
            base_multiple -= 0.5

        # Ensure reasonable bounds
        base_multiple = max(2.0, min(15.0, base_multiple))

        # Purchase Multiple Valuation
        pm_value = revenue * base_multiple

        # DCF Analysis
        growth_rate = 0.03  # 3% conservative growth
        discount_rate = 0.082  # 8.2% discount rate
        projection_years = 10

        # Project future cash flows
        total_pv = 0
        for year in range(1, projection_years + 1):
            year_growth = growth_rate * (0.9 ** (year - 1))
            projected_revenue = revenue * ((1 + year_growth) ** year)
            discount_factor = 1 / ((1 + discount_rate) ** year)
            pv = projected_revenue * discount_factor
            total_pv += pv

        # Terminal value
        terminal_growth = 0.02
        terminal_value = (
            revenue * ((1 + growth_rate) ** projection_years) * (1 + terminal_growth)
        ) / (discount_rate - terminal_growth)
        terminal_pv = terminal_value / ((1 + discount_rate) ** projection_years)

        dcf_value = total_pv + terminal_pv

        # Weighted valuation (60% PM, 40% DCF)
        weighted_value = (pm_value * 0.6) + (dcf_value * 0.4)

        return {
            "base_multiple": base_multiple,
            "pm_value": pm_value,
            "dcf_value": dcf_value,
            "weighted_value": weighted_value,
        }

    # Calculate total catalog value from lifecycle valuations
    total_lifecycle_value = sum(track["value"] for track in track_valuations)
    total_lifecycle_pub_value = sum(
        track["publishing_value"] for track in track_valuations
    )
    total_lifecycle_master_value = sum(
        track["master_value"] for track in track_valuations
    )

    # Calculate separate valuations for publishing and master (for compatibility/fallback)
    pub_valuation = calculate_valuation(annual_pub_revenue, avg_pub_ownership)
    master_valuation = calculate_valuation(annual_master_revenue, avg_master_ownership)

    # Use lifecycle value as the primary valuation
    base_multiple = pub_valuation["base_multiple"]  # Use same multiple for reporting
    pm_value = pub_valuation["pm_value"] + master_valuation["pm_value"]
    dcf_value = pub_valuation["dcf_value"] + master_valuation["dcf_value"]

    # Use lifecycle-based valuation as the estimated value
    if track_valuations:
        weighted_value = total_lifecycle_value
        pub_estimated_value = total_lifecycle_pub_value
        master_estimated_value = total_lifecycle_master_value
    else:
        weighted_value = (
            pub_valuation["weighted_value"] + master_valuation["weighted_value"]
        )
        pub_estimated_value = pub_valuation["weighted_value"]
        master_estimated_value = master_valuation["weighted_value"]

    return {
        "estimated_value": int(round(weighted_value)),
        "annual_revenue": int(round(annual_revenue)),
        "track_count": track_count,
        "total_streams": total_streams,
        "catalog_age": round(catalog_age, 1),
        "valuation_range": {
            "low": round(min(pm_value, dcf_value), 2),
            "mid": round(weighted_value, 2),
            "high": round(max(pm_value, dcf_value), 2),
        },
        "purchase_multiple": round(base_multiple, 2),
        "purchase_multiple_value": int(round(pm_value)),
        "dcf_value": int(round(dcf_value)),
        # Publishing-specific values (lifecycle-aware)
        "publishing": {
            "annual_revenue": int(round(annual_pub_revenue)),
            "estimated_value": int(round(pub_estimated_value)),
            "purchase_multiple_value": int(round(pub_valuation["pm_value"])),
            "dcf_value": int(round(pub_valuation["dcf_value"])),
            "valuation_range": {
                "low": round(
                    min(pub_valuation["pm_value"], pub_valuation["dcf_value"]), 2
                ),
                "mid": round(pub_estimated_value, 2),
                "high": round(
                    max(pub_valuation["pm_value"], pub_valuation["dcf_value"]), 2
                ),
            },
        },
        # Master-specific values (lifecycle-aware)
        "master": {
            "annual_revenue": int(round(annual_master_revenue)),
            "estimated_value": int(round(master_estimated_value)),
            "purchase_multiple_value": int(round(master_valuation["pm_value"])),
            "dcf_value": int(round(master_valuation["dcf_value"])),
            "valuation_range": {
                "low": round(
                    min(master_valuation["pm_value"], master_valuation["dcf_value"]), 2
                ),
                "mid": round(master_estimated_value, 2),
                "high": round(
                    max(master_valuation["pm_value"], master_valuation["dcf_value"]), 2
                ),
            },
        },
        # Lifecycle insights (new)
        "lifecycle": {"distribution": lifecycle_breakdown, "tracks": track_valuations},
        # last_updated is already an ISO string from the new dict response
        "last_updated": last_updated if last_updated else None,
    }


@catalog_router.get("/export/schedule-a")
async def export_schedule_a(
    db: Session = Depends(get_session),
    user: Optional[User] = Depends(get_user),
):
    """
    Export catalog as Schedule A CSV with revenue calculations.
    Returns CSV file with: Song, Artist, Title, Master %, Pub %, Master Rev, Pub Rev
    """
    import traceback
    from datetime import date
    from io import StringIO

    from fastapi.responses import StreamingResponse

    try:
        if not user:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated"
            )

        # Get user's catalog
        catalog_items = (
            db.query(UserCatalog, Songs)
            .join(Songs, UserCatalog.song_id == Songs.id)
            .filter(UserCatalog.user_id == user.id)
            .all()
        )

        if not catalog_items:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="No tracks in catalog"
            )

        # Revenue rates
        MASTER_RATE = 3.8  # per 1000 streams
        PUB_RATE = 1.0  # per 1000 streams

        # Create CSV
        output = StringIO()

        # Write headers
        headers = [
            "Song",
            "Artist",
            "Title",
            "Master %",
            "Pub %",
            "Master Rev of Song",
            "Pub Rev of Song",
        ]
        output.write(",".join(headers) + "\n")

        # Track totals
        total_master_revenue = 0.0
        total_pub_revenue = 0.0

        # Fetch latest playcount data for all tracks using crud/stats.py
        track_id_to_streams = {}
        spotify_track_ids = [song.spotify_track_id for _, song in catalog_items]

        if spotify_track_ids:
            from datetime import timedelta

            today = date.today()
            window_start = today - timedelta(days=7)  # Last 7 days to get latest

            try:
                # Use the efficient catalog helper that reads from StatsCache directly
                latest_playcounts = await stats_crud.get_latest_playcounts_for_catalog(
                    track_ids=spotify_track_ids, user=user, db=db, days_back=7
                )

                # Extract total playcount for each track
                for track_id, data in latest_playcounts.items():
                    track_id_to_streams[track_id] = data.get("total_playcount", 0)

            except Exception as e:
                logger.error(f"Error fetching playcount data for export: {e}")

        # Write data rows
        for catalog_item, song in catalog_items:
            # Get total streams from our fetched data
            total_streams = track_id_to_streams.get(song.spotify_track_id, 0)

            # Get royalty percentages
            master_equity = catalog_item.master_royalty or 0
            pub_equity = catalog_item.publishing_royalty or 0

            # Calculate revenues
            master_revenue = (total_streams / 1000) * MASTER_RATE * master_equity
            pub_revenue = (total_streams / 1000) * PUB_RATE * pub_equity

            # Add to totals
            total_master_revenue += master_revenue
            total_pub_revenue += pub_revenue

            # Escape quotes in strings
            artist = (song.artist or "").replace('"', '""')
            title = (song.title or "").replace('"', '""')
            song_name = f'"{artist} - {title}"'

            # Build row
            row = [
                song_name,
                f'"{artist}"',
                f'"{title}"',
                f"{int(master_equity * 100)}%",
                f"{int(pub_equity * 100)}%",
                f"${master_revenue:.2f}",
                f"${pub_revenue:.2f}",
            ]

            output.write(",".join(row) + "\n")

        # Write summary rows
        output.write("\n")  # Empty line before summary
        output.write(f",,,,Total Master Revenue,${total_master_revenue:.2f},\n")
        output.write(f",,,,Total Publishing Revenue,,${total_pub_revenue:.2f}\n")
        output.write(
            f",,,,Total Revenue,${total_master_revenue + total_pub_revenue:.2f},\n"
        )

        # Reset string buffer position
        output.seek(0)

        # Return as downloadable CSV
        filename = f"schedule_a_{date.today().isoformat()}.csv"

        return StreamingResponse(
            iter([output.getvalue()]),
            media_type="text/csv",
            headers={
                "Content-Disposition": f'attachment; filename="{filename}"',
                "Cache-Control": "no-cache",
            },
        )
    except Exception as e:
        logger.error(f"Error in export_schedule_a: {str(e)}")
        logger.error(traceback.format_exc())
        raise HTTPException(status_code=500, detail=f"Export failed: {str(e)}")
