"""
SSE endpoint for streaming Genius artist song fetching with real-time progress updates.
Allows frontend to close modal while fetch continues in background.
"""
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session
from app.models.models import User
from app.settings.settings import get_settings
from app.routers.auth import get_user
from app.database.session import get_session
from app.schemas.search_result import SearchResult
from datetime import datetime
import requests
import json
import asyncio
from typing import AsyncGenerator

genius_stream_router = APIRouter(tags=["Genius Stream"], prefix="/genius-stream")
settings = get_settings()


def _genius_get(url, headers, params=None, timeout=30):
    """Synchronous HTTP GET for Genius API (to be run in thread pool)."""
    return requests.get(url, headers=headers, params=params, timeout=timeout)


def _check_producer_credits(song_detail, artist_id, artist_name):
    """Check if the given artist has producer credits on a song detail response."""
    # Check producer_artists
    producer_artists = song_detail.get("producer_artists", [])
    if any(p.get("id") == artist_id for p in producer_artists):
        return True

    # Check custom_performances
    custom_performances = song_detail.get("custom_performances", [])
    for performance in custom_performances:
        label = performance.get("label", "").lower()
        if "produc" in label:
            for artist in performance.get("artists", []):
                if artist.get("id") == artist_id:
                    return True

    # Check writer_artists as fallback
    writer_artists = song_detail.get("writer_artists", [])
    if any(w.get("id") == artist_id for w in writer_artists):
        description = song_detail.get("description", {}).get("plain", "").lower()
        if "produced by" in description and artist_name.lower() in description:
            return True

    return False


def _extract_track_info(song_detail, song):
    """Extract track info from a song detail response."""
    song_primary_artist = song_detail.get("primary_artist", {})
    performing_artist = song_primary_artist.get("name", "") or "Unknown Artist"
    song_title = song_detail.get("title", "") or song.get("title", "") or "Unknown Title"

    try:
        release_date = song_detail.get("release_date_for_display", "")
        if release_date:
            date_added = datetime.strptime(release_date, "%B %d, %Y").strftime("%Y-%m-%d")
        else:
            date_added = datetime.now().strftime("%Y-%m-%d")
    except (ValueError, AttributeError):
        date_added = datetime.now().strftime("%Y-%m-%d")

    album_data = song_detail.get("album")
    album_name = "N/A"
    if album_data and isinstance(album_data, dict):
        album_name = album_data.get("name", "") or "N/A"

    album_art = song_detail.get("song_art_image_url", "") or "N/A"

    return {
        "date_added": date_added,
        "title": song_title,
        "is_infringement": False,
        "artist": performing_artist,
        "album": album_name,
        "isrc": "N/A",
        "album_art": album_art,
        "spotify_track_id": "",
        "spotify_popularity": 0,
    }


async def _fetch_song_detail(song_id, headers):
    """Fetch a single song's detail from Genius API."""
    try:
        resp = await asyncio.to_thread(
            _genius_get, f"https://api.genius.com/songs/{song_id}", headers, None, 15
        )
        if resp.ok:
            return resp.json().get("response", {}).get("song", {})
    except requests.Timeout:
        pass
    return None


async def generate_genius_stream(artist_id: int, artist_name: str) -> AsyncGenerator[str, None]:
    """Generator that streams progress updates as SSE events."""
    print(f"[GeniusSSE] Starting stream for artist_id={artist_id}, artist_name={artist_name}")
    headers = {"Authorization": f"Bearer {settings.genius_bearer_token}"}

    # Send initial status
    yield f"data: {json.dumps({'type': 'status', 'message': f'Starting fetch for {artist_name}...', 'progress': 0})}\n\n"

    PROXY_BASE = "https://api-staging.verax.app/genius-proxy"
    track_list = []
    page = 1
    per_page = 50
    total_processed = 0
    batch_size = 10  # Fetch 10 song details concurrently
    use_proxy = True

    while True:

        try:
            if use_proxy:
                songs_response = await asyncio.to_thread(
                    lambda p=page: requests.get(
                        f"{PROXY_BASE}/artists/{artist_id}/songs",
                        params={"page": p, "sort": "popularity"},
                        timeout=15,
                    )
                )
            else:
                artist_songs_url = f"https://api.genius.com/artists/{artist_id}/songs"
                songs_payload = {"per_page": per_page, "page": page, "sort": "popularity"}
                songs_response = await asyncio.to_thread(
                    _genius_get, artist_songs_url, headers, songs_payload, 30
                )
        except (requests.Timeout, Exception) as e:
            if use_proxy and page == 1:
                print(f"[GeniusSSE] Proxy failed, falling back to public API: {e}")
                use_proxy = False
                continue
            yield f"data: {json.dumps({'type': 'error', 'message': 'Request timeout while fetching song list'})}\n\n"
            return

        if not songs_response.ok:
            if use_proxy and page == 1:
                print(f"[GeniusSSE] Proxy returned {songs_response.status_code}, falling back to public API")
                use_proxy = False
                continue
            yield f"data: {json.dumps({'type': 'error', 'message': f'Genius API error: {songs_response.status_code}'})}\n\n"
            return

        songs_data = songs_response.json()
        songs = songs_data.get("response", {}).get("songs", [])

        if not songs:
            break

        # Process songs in concurrent batches
        for batch_start in range(0, len(songs), batch_size):
            batch = songs[batch_start:batch_start + batch_size]
            valid_songs = [(s, s.get("id"), s.get("title", "Unknown")) for s in batch if s.get("id")]

            total_processed += len(valid_songs)
            yield f"data: {json.dumps({'type': 'progress', 'message': f'Checking songs {total_processed - len(valid_songs) + 1}-{total_processed}...', 'current': total_processed, 'song_title': valid_songs[-1][2] if valid_songs else ''})}\n\n"

            # Fetch all song details in this batch concurrently
            detail_tasks = [_fetch_song_detail(sid, headers) for _, sid, _ in valid_songs]
            details = await asyncio.gather(*detail_tasks)

            # Process results
            for (song, song_id, song_title), song_detail in zip(valid_songs, details):
                if not song_detail:
                    continue

                if not _check_producer_credits(song_detail, artist_id, artist_name):
                    continue

                track_info = _extract_track_info(song_detail, song)
                print(f"[GeniusSSE] Found: '{track_info['title']}' by {track_info['artist']}")
                track_list.append(track_info)

                yield f"data: {json.dumps({'type': 'song_found', 'song': track_info, 'total_found': len(track_list)})}\n\n"

        page += 1
        next_page = songs_data.get("response", {}).get("next_page")
        if not next_page:
            break

    print(f"[GeniusSSE] Stream complete. Found {len(track_list)} songs total.")
    # Send completion
    yield f"data: {json.dumps({'type': 'complete', 'songs': track_list, 'total': len(track_list), 'message': f'Found {len(track_list)} songs with production credits'})}\n\n"


@genius_stream_router.get("/fetch")
async def stream_genius_fetch(
    artist_url: str = Query(..., description="Genius artist URL"),
    user: User = Depends(get_user),
    db: Session = Depends(get_session),
):
    """
    Stream Genius artist song fetch with real-time progress updates via SSE.
    """
    import re
    print(f"[GeniusSSE] /fetch endpoint called with artist_url={artist_url}")

    PROXY_BASE = "https://api-staging.verax.app/genius-proxy"

    # Extract artist slug or numeric ID from URL
    artist_id = None
    artist_name = None
    slug_match = re.search(r'/artists/([^/?#]+)', artist_url, re.IGNORECASE)

    if not slug_match:
        print(f"[GeniusSSE] Could not parse artist from URL: {artist_url}")
        async def error_stream():
            yield f"data: {json.dumps({'type': 'error', 'message': 'Invalid Genius artist URL.'})}\n\n"
        return StreamingResponse(error_stream(), media_type="text/event-stream")

    artist_slug = slug_match.group(1).strip()

    if artist_slug.isdigit():
        artist_id = int(artist_slug)
    else:
        # Strategy 1: Genius artist search via Ionos proxy (bypasses Cloudflare on Render)
        print(f"[GeniusSSE] Resolving artist slug '{artist_slug}' to ID via proxy...")
        try:
            search_resp = await asyncio.to_thread(
                lambda: requests.get(
                    f"{PROXY_BASE}/search/artist",
                    params={"q": artist_slug.replace("-", " ")},
                    timeout=10,
                )
            )
            if search_resp.ok:
                url_slug = artist_slug.lower()
                for section in search_resp.json().get("response", {}).get("sections", []):
                    for hit in section.get("hits", []):
                        result = hit.get("result", {})
                        a_url = result.get("url", "")
                        if a_url:
                            a_slug = a_url.rstrip("/").split("/")[-1].lower()
                            if a_slug == url_slug:
                                artist_id = result.get("id")
                                artist_name = result.get("name")
                                print(f"[GeniusSSE] Proxy resolved slug '{artist_slug}' -> artist_id={artist_id}")
                                break
                    if artist_id:
                        break
        except Exception as e:
            print(f"[GeniusSSE] Proxy artist search failed: {e}")

        # Strategy 2: Fallback — fetch Genius page directly (works locally, may fail on cloud)
        if not artist_id:
            try:
                page_resp = await asyncio.to_thread(
                    lambda: requests.get(
                        f"https://genius.com/artists/{artist_slug}",
                        timeout=10,
                        headers={
                            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
                        },
                    )
                )
                if page_resp.ok:
                    id_match = re.search(r'artists/(\d+)', page_resp.text)
                    if id_match:
                        artist_id = int(id_match.group(1))
                        print(f"[GeniusSSE] Direct page resolved slug '{artist_slug}' -> artist_id={artist_id}")
            except Exception as e:
                print(f"[GeniusSSE] Direct page resolve failed: {e}")

        # Strategy 3: Fallback song search via public API
        if not artist_id:
            fallback_name = artist_slug.replace("-", " ")
            try:
                api_headers = {"Authorization": f"Bearer {settings.genius_bearer_token}"}
                search_res = await asyncio.to_thread(
                    _genius_get, "https://api.genius.com/search", api_headers, {"q": fallback_name}
                )
                if search_res.ok:
                    name_lower = fallback_name.lower()
                    for hit in search_res.json().get("response", {}).get("hits", []):
                        for a in [hit.get("result", {}).get("primary_artist", {})] + (hit.get("result", {}).get("featured_artists") or []):
                            if a.get("name", "").lower() == name_lower:
                                artist_id = a.get("id")
                                artist_name = a.get("name")
                                print(f"[GeniusSSE] Song search resolved '{fallback_name}' -> artist_id={artist_id}")
                                break
                        if artist_id:
                            break
            except Exception as e:
                print(f"[GeniusSSE] Song search fallback failed: {e}")

    if not artist_id:
        print(f"[GeniusSSE] Could not resolve artist ID for slug '{artist_slug}'")
        async def error_stream():
            yield f"data: {json.dumps({'type': 'error', 'message': 'Could not find artist. Please check the URL.'})}\n\n"
        return StreamingResponse(error_stream(), media_type="text/event-stream")

    # Get artist name from API
    api_headers = {"Authorization": f"Bearer {settings.genius_bearer_token}"}
    verify_resp = await asyncio.to_thread(
        _genius_get, f"https://api.genius.com/artists/{artist_id}", api_headers
    )
    if verify_resp.ok:
        artist_data = verify_resp.json().get("response", {}).get("artist", {})
        artist_name = artist_data.get("name", f"Artist {artist_id}")
    else:
        artist_name = artist_slug.replace("-", " ").title()

    print(f"[GeniusSSE] Found artist_id={artist_id}, artist_name={artist_name}, starting stream")
    return StreamingResponse(
        generate_genius_stream(artist_id, artist_name),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        }
    )
