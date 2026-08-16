from fastapi import APIRouter, Depends, HTTPException, status, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session
from sqlalchemy import or_
from app.models.models import User
from app.settings.settings import get_settings
from app.routers.auth import get_user, oauth2_bearer
from app.database.session import get_session
from app.misc import get_spotify_access_token
from app.schemas.search_result import SearchResult
from app.models.models import UserCatalog, Songs
from typing import List, Literal, Optional
import requests
from datetime import datetime
from jose import jwt, JWTError

search_router = APIRouter(tags=["Search"], prefix="/search")
settings = get_settings()
spotify_access_token = None


async def get_user_optional(
    token: Optional[str] = Depends(oauth2_bearer), db: Session = Depends(get_session)
) -> Optional[User]:
    """Optional authentication - returns None if no valid token in development mode"""
    if not token and settings.mode == "development":
        return None

    if not token:
        raise HTTPException(status_code=403, detail="Token is invalid or expired")

    try:
        from app.routers.auth import SECRET_KEY, ALGORITHM

        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        username: str = payload.get("sub")
        if username is None:
            if settings.mode == "development":
                return None
            raise HTTPException(status_code=403, detail="Token is invalid or expired")
        user = db.query(User).filter(User.username == username).first()
        if not user and settings.mode != "development":
            raise HTTPException(status_code=403, detail="Token is invalid or expired")
        return user
    except JWTError:
        if settings.mode == "development":
            return None
        raise HTTPException(status_code=403, detail="Token is invalid or expired")


@search_router.get(
    "", status_code=status.HTTP_200_OK, response_model=List[SearchResult]
)
async def search(
    request: Request,
    keyword: str,
    type: Literal["catalog", "spotify", "genius"] = "catalog",
    limit: int = 10,
    offset: int = 0,
    db: Session = Depends(get_session),
    user: Optional[User] = Depends(get_user_optional),
):
    # For catalog and genius searches, user is required
    if type in ["catalog", "genius"] and not user:
        raise HTTPException(
            status_code=403, detail="Authentication required for this search type"
        )

    if type == "catalog":
        return search_catalog(keyword, limit, offset, db, user)
    elif type == "spotify":
        return search_spotify(request, keyword, limit, offset)
    elif type == "genius":
        return search_genius(keyword, user, db)
    else:
        raise HTTPException(status_code=400, detail="Type is not supported.")


def search_catalog(
    keyword: str,
    limit: int,
    offset: int,
    db: Session = Depends(get_session),
    user: User = Depends(get_user),
):
    """Search user's catalog using new UserCatalog + Songs architecture"""
    # Query Songs through UserCatalog
    results = (
        db.query(Songs)
        .join(UserCatalog, UserCatalog.song_id == Songs.id)
        .filter(UserCatalog.user_id == user.id)
        .filter(
            or_(
                Songs.artist.ilike(f"%{keyword}%"),
                Songs.title.ilike(f"%{keyword}%"),
                Songs.album.ilike(f"%{keyword}%"),
            )
        )
        .offset(offset)
        .limit(limit)
        .all()
    )

    # Convert Songs to SearchResult format for compatibility
    search_results = []
    for song in results:
        # Get the UserCatalog entry for date_added and is_infringement
        user_catalog = db.query(UserCatalog).filter(
            UserCatalog.user_id == user.id,
            UserCatalog.song_id == song.id
        ).first()

        search_results.append(SearchResult(
            date_added=user_catalog.date_added if user_catalog else datetime.now(),
            title=song.title,
            is_infringement=user_catalog.is_infringement if user_catalog else False,
            artist=song.artist,
            album=song.album,
            isrc=song.isrc,
            album_art=song.album_art,
            spotify_track_id=song.spotify_track_id,
            spotify_popularity=0,  # Not stored in Songs table
        ))

    return search_results


def search_spotify(request: Request, keyword: str, limit: int = 10, offset: int = 0):

    global spotify_access_token
    if not spotify_access_token:
        spotify_access_token = get_spotify_access_token()
    url = "https://api.spotify.com/v1/search"
    headers = {"Authorization": f"Bearer {spotify_access_token}"}
    payload = {"q": keyword, "type": ["track"], "limit": limit, "offset": offset}

    response = requests.get(url, headers=headers, params=payload)
    detail_info = response.json()
    if not response.ok:
        if detail_info["error"]["status"] == 401:
            spotify_access_token = get_spotify_access_token()
            return RedirectResponse(url=str(request.url))
        raise HTTPException(
            status_code=detail_info["error"]["status"],
            detail=detail_info["error"]["message"],
        )

    tracks = response.json()["tracks"]["items"]

    track_list = []
    for track in tracks:
        # sometimes the string is not valid, e.g. only the year will be returned
        try:
            date_added = datetime.fromisoformat(track["album"]["release_date"]).date()
        except ValueError:
            date_added = datetime.now()

        track_information = SearchResult(
            date_added=date_added,
            title=track["name"],
            is_infringement=False,
            artist=", ".join(
                artist["name"] for artist in track["artists"]
            ),  # for collabs
            album=track["album"]["name"],
            isrc=track["external_ids"].get(
                "isrc", "N/A"
            ),  # looking for isrc if not there not available
            album_art=(
                track["album"]["images"][0]["url"]
                if track["album"]["images"][0]["url"]
                else "N/A"
            ),
            spotify_track_id=track["id"],
            spotify_popularity=track.get("popularity", 0),
        )
        track_list.append(track_information)
    return track_list


@search_router.get("/spotify/track/{track_id}")
async def get_spotify_track(
    track_id: str,
    request: Request,
    user: Optional[User] = Depends(get_user_optional),
    db: Session = Depends(get_session),
):
    """
    Get Spotify track details by track ID.
    Returns album art, title, artist, and other track metadata.
    """
    global spotify_access_token
    if not spotify_access_token:
        spotify_access_token = get_spotify_access_token()

    url = f"https://api.spotify.com/v1/tracks/{track_id}"
    headers = {"Authorization": f"Bearer {spotify_access_token}"}

    response = requests.get(url, headers=headers)

    if response.status_code == 401:
        # Token expired, refresh and retry
        spotify_access_token = get_spotify_access_token()
        headers = {"Authorization": f"Bearer {spotify_access_token}"}
        response = requests.get(url, headers=headers)

    if not response.ok:
        raise HTTPException(
            status_code=response.status_code,
            detail=f"Failed to fetch track from Spotify: {response.text}"
        )

    track = response.json()

    # Get album art (largest image first)
    album_images = track.get("album", {}).get("images", [])
    album_art = album_images[0]["url"] if album_images else None

    return {
        "id": track["id"],
        "title": track["name"],
        "artist": ", ".join(artist["name"] for artist in track.get("artists", [])),
        "album": track.get("album", {}).get("name"),
        "album_art": album_art,
        "isrc": track.get("external_ids", {}).get("isrc"),
        "duration_ms": track.get("duration_ms"),
        "popularity": track.get("popularity"),
        "preview_url": track.get("preview_url"),
        "spotify_url": track.get("external_urls", {}).get("spotify"),
    }


def search_genius(
    keyword: str, user: User, db: Session, limit: int = 10, offset: int = 0
):
    import re

    # First, check if keyword is a direct Genius URL
    # This helps with producers/writers who don't show up in search
    artist_slug = None
    artist_id = None

    if "genius.com/artists/" in keyword.lower():
        # Extract artist slug/ID from URL
        url_parts = keyword.split("/")
        for i, part in enumerate(url_parts):
            if part == "artists" and i + 1 < len(url_parts):
                artist_slug = url_parts[i + 1].strip()
                # Clean up any query params or fragments
                artist_slug = artist_slug.split("?")[0].split("#")[0].split("/")[0]

                # Check if it's a numeric ID
                if artist_slug.isdigit():
                    artist_id = int(artist_slug)
                    print(f"🔍 Extracted artist ID from URL: {artist_id}")
                    # Try to get artist info directly
                    headers = {
                        "Authorization": f"Bearer {settings.genius_bearer_token}"
                    }
                    verify_url = f"https://api.genius.com/artists/{artist_id}"
                    verify_response = requests.get(verify_url, headers=headers)
                    if verify_response.ok:
                        verified_artist = (
                            verify_response.json().get("response", {}).get("artist", {})
                        )
                        artist_name = verified_artist.get("name", keyword)
                        print(f"✅ Found artist via ID: {artist_name}")
                        return fetch_artist_songs(artist_id, artist_name, headers)
                break

    # If we have a slug from URL but not numeric ID, use it as the search term
    if artist_slug and not artist_id:
        # Convert slug to readable name for search (e.g., "Dream-awake" -> "Dream awake")
        keyword = artist_slug.replace("-", " ")
        print(f"🔍 Extracted artist name from URL slug: {keyword}")

    # Try different search variations for producers
    # First try exact search
    search_variations = [keyword]

    # If it looks like a producer name, also try with common producer suffixes
    if not any(
        term in keyword.lower() for term in ["https://", "genius.com", "http://"]
    ):
        # Add variations that might help find producers
        search_variations.extend(
            [
                f"{keyword} producer",
                f"{keyword} production",
                keyword.replace(" ", "-"),  # Try hyphenated version
                keyword.replace("-", " "),  # Try space version
            ]
        )

    search_url = f"https://api.genius.com/search/"
    headers = {"Authorization": f"Bearer {settings.genius_bearer_token}"}

    all_hits = []
    for search_term in search_variations:
        print(f"🔍 Trying Genius search: {search_term}")
        search_payload = {"q": search_term}
        search_response = requests.get(
            search_url, headers=headers, params=search_payload
        )

        if search_response.ok:
            search_data = search_response.json()
            hits = search_data.get("response", {}).get("hits", [])
            if hits:
                all_hits.extend(hits)
                # Stop if we found hits
                break

    # Continue with original search response from the last attempt

    if not search_response.ok:
        raise HTTPException(
            status_code=search_response.status_code,
            detail=f"Genius API error: {search_response.text}",
        )

    # Use all_hits if we found any, otherwise use empty list
    hits = all_hits

    if not hits:
        return []

    # Find the best matching artist by comparing names
    artist_id = None
    artist_name = None
    keyword_lower = keyword.lower().strip()

    # Try to find exact match in primary artists first
    for hit in hits:
        primary_artist = hit.get("result", {}).get("primary_artist", {})
        current_artist_name = primary_artist.get("name", "")

        # Check if artist name matches (case-insensitive)
        if current_artist_name.lower().strip() == keyword_lower:
            artist_id = primary_artist.get("id")
            artist_name = current_artist_name
            break

    # If no exact match in primary artists, check producer/writer credits in more hits
    if not artist_id:
        # Normalize the search term for better matching
        keyword_normalized = keyword_lower.replace(" ", "").replace("-", "")

        print(f"🔍 Checking producer/writer credits for: {keyword}")

        # Check more songs for producer/writer credits (up to 15)
        for hit in hits[:15]:
            song_id = hit.get("result", {}).get("id")
            if song_id:
                # Fetch full song details to check producer credits
                song_url = f"https://api.genius.com/songs/{song_id}"
                song_response = requests.get(song_url, headers=headers)
                if song_response.ok:
                    song_data = song_response.json().get("response", {}).get("song", {})

                    # Check producer_artists
                    producer_artists = song_data.get("producer_artists", [])
                    for producer in producer_artists:
                        producer_name = producer.get("name", "")
                        producer_normalized = (
                            producer_name.lower()
                            .strip()
                            .replace(" ", "")
                            .replace("-", "")
                        )

                        # More flexible matching
                        if (
                            producer_normalized == keyword_normalized
                            or keyword_normalized in producer_normalized
                            or producer_normalized in keyword_normalized
                        ):
                            artist_id = producer.get("id")
                            artist_name = producer_name
                            print(
                                f"✅ Found producer match: {artist_name} (ID: {artist_id})"
                            )
                            break

                    # Check custom_performances for production credits
                    if not artist_id:
                        custom_performances = song_data.get("custom_performances", [])
                        for performance in custom_performances:
                            label = performance.get("label", "").lower()
                            # Check if this is a production-related role
                            if "produc" in label:
                                for artist in performance.get("artists", []):
                                    perf_name = artist.get("name", "")
                                    perf_normalized = (
                                        perf_name.lower()
                                        .strip()
                                        .replace(" ", "")
                                        .replace("-", "")
                                    )

                                    if (
                                        perf_normalized == keyword_normalized
                                        or keyword_normalized in perf_normalized
                                        or perf_normalized in keyword_normalized
                                    ):
                                        artist_id = artist.get("id")
                                        artist_name = perf_name
                                        print(
                                            f"✅ Found producer in custom_performances: {artist_name} (ID: {artist_id})"
                                        )
                                        break
                            if artist_id:
                                break

                    # Check writer_artists if still not found
                    if not artist_id:
                        writer_artists = song_data.get("writer_artists", [])
                        for writer in writer_artists:
                            writer_name = writer.get("name", "")
                            writer_normalized = (
                                writer_name.lower()
                                .strip()
                                .replace(" ", "")
                                .replace("-", "")
                            )

                            # More flexible matching
                            if (
                                writer_normalized == keyword_normalized
                                or keyword_normalized in writer_normalized
                                or writer_normalized in keyword_normalized
                            ):
                                artist_id = writer.get("id")
                                artist_name = writer_name
                                print(
                                    f"✅ Found writer match: {artist_name} (ID: {artist_id})"
                                )
                                break

                    if artist_id:
                        break

    # For producer searches, we should NOT use fallback to unrelated artists
    # Only use exact matches to avoid showing wrong artist's songs
    if not artist_id:
        print(f"⚠️ No exact match found for artist/producer: {keyword}")
        # Return empty list - the frontend will show "No songs found"
        return []

    return fetch_artist_songs(artist_id, artist_name, headers)


def fetch_artist_songs(artist_id: int, artist_name: str, headers: dict):
    from app.misc import get_spotify_access_token

    print(f"🎵 Fetching songs for artist ID {artist_id} ({artist_name})")

    # Fetch ALL songs for this artist with production credits (paginate through all)
    track_list = []
    page = 1
    per_page = 50  # Fetch more per page for efficiency

    while True:
        artist_songs_url = f"https://api.genius.com/artists/{artist_id}/songs"
        songs_payload = {"per_page": per_page, "page": page, "sort": "popularity"}
        songs_response = requests.get(
            artist_songs_url, headers=headers, params=songs_payload
        )

        if not songs_response.ok:
            break

        songs_data = songs_response.json()
        songs = songs_data.get("response", {}).get("songs", [])

        if not songs:
            break

        for song in songs:
            # Fetch full song details to check production credits
            song_id = song.get("id")
            if not song_id:
                continue

            print(
                f"  📝 Processing song {len(track_list) + 1}: {song.get('title', 'Unknown')}"
            )
            song_detail_url = f"https://api.genius.com/songs/{song_id}"
            song_detail_response = requests.get(song_detail_url, headers=headers)

            if not song_detail_response.ok:
                continue

            song_detail = (
                song_detail_response.json().get("response", {}).get("song", {})
            )

            # Check if artist is in production credits (check multiple fields)
            is_producer = False

            # Check producer_artists
            producer_artists = song_detail.get("producer_artists", [])
            is_producer = any(p.get("id") == artist_id for p in producer_artists)

            # Check custom_performances for production credits
            if not is_producer:
                custom_performances = song_detail.get("custom_performances", [])
                for performance in custom_performances:
                    label = performance.get("label", "").lower()
                    if "produc" in label:
                        for artist in performance.get("artists", []):
                            if artist.get("id") == artist_id:
                                is_producer = True
                                break
                    if is_producer:
                        break

            # Check writer_artists as fallback (some producers are listed as writers)
            if not is_producer:
                writer_artists = song_detail.get("writer_artists", [])
                # Only count as producer if the artist name suggests production role
                if any(w.get("id") == artist_id for w in writer_artists):
                    # Verify through song title or description if this is actually a production credit
                    description = (
                        song_detail.get("description", {}).get("plain", "").lower()
                    )
                    if (
                        "produced by" in description
                        and artist_name.lower() in description
                    ):
                        is_producer = True

            # Skip if the artist is not actually a producer on this track
            if not is_producer:
                print(
                    f"  ⏭️  Skipping '{song_detail.get('title')}' - no producer credit found for {artist_name}"
                )
                continue

            # Get the actual performing artist name (not the producer/writer)
            song_primary_artist = song_detail.get("primary_artist", {})
            performing_artist = song_primary_artist.get("name", "Unknown Artist")

            # Parse release date
            try:
                release_date = song_detail.get("release_date_for_display", "")
                if release_date:
                    date_added = datetime.strptime(release_date, "%B %d, %Y").date()
                else:
                    date_added = datetime.now().date()
            except (ValueError, AttributeError):
                date_added = datetime.now().date()

            track_information = SearchResult(
                date_added=date_added,
                title=song_detail.get("title", "Unknown Title"),
                is_infringement=False,
                artist=performing_artist,  # Use the actual performing artist
                album=(
                    song_detail.get("album", {}).get("name", "N/A")
                    if song_detail.get("album")
                    else "N/A"
                ),
                isrc="N/A",  # Genius API doesn't provide ISRC
                album_art=song_detail.get("song_art_image_url", "N/A"),
                spotify_track_id="",  # Will be filled by Spotify search
                spotify_popularity=0,  # Default, will be updated
            )
            track_list.append(track_information)

        page += 1

        # Check if there are more pages
        next_page = songs_data.get("response", {}).get("next_page")
        if not next_page:
            break

    # Skip Spotify enrichment during initial fetch for faster response
    # The frontend will search Spotify during import to get ISRCs and accurate metadata
    print(
        f"✅ Fetched {len(track_list)} songs with production credits from Genius for artist: {artist_name}"
    )

    return track_list
