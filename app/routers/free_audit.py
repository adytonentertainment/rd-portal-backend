"""
Free Audit Endpoints (Public / No Auth Required)

Provides catalog fetching from Genius/Spotify and MLC audit
for the free audit flow on the landing page.
"""
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, EmailStr
from typing import List, Optional
import logging
import asyncio
import re
import requests
import html as html_lib

from app.settings.settings import get_settings
from app.libs.MLC.mlc_api import MLCClient
from app.misc.helper import get_spotify_access_token
from app.services.audit_report_pdf import AuditReportPDFGenerator

logger = logging.getLogger(__name__)

free_audit_router = APIRouter(prefix="/free-audit", tags=["Free Audit"])
settings = get_settings()

# ─── In-memory cache for Genius song details (avoids rate limits) ──
from functools import lru_cache
import time

_song_detail_cache: dict = {}
_CACHE_TTL = 3600 * 6  # 6 hours


def _get_cached_song_detail(song_id: int):
    entry = _song_detail_cache.get(song_id)
    if entry and (time.time() - entry["ts"]) < _CACHE_TTL:
        return entry["data"]
    return None


def _set_cached_song_detail(song_id: int, data):
    _song_detail_cache[song_id] = {"data": data, "ts": time.time()}
    # Evict old entries if cache grows too large
    if len(_song_detail_cache) > 5000:
        cutoff = time.time() - _CACHE_TTL
        to_remove = [k for k, v in _song_detail_cache.items() if v["ts"] < cutoff]
        for k in to_remove:
            del _song_detail_cache[k]


# ─── Genius catalog fetch ──────────────────────────────────────────

GENIUS_PROXY_BASE = "https://api-staging.verax.app/genius-proxy"


def _genius_get(url, headers, params=None, timeout=30):
    return requests.get(url, headers=headers, params=params, timeout=timeout)


def _resolve_artist_from_page(page_url: str):
    """Scrape the Genius artist page to extract artist ID and name from meta tags."""
    try:
        res = requests.get(page_url, timeout=15, headers={
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.5",
        })
        if not res.ok:
            return None, None
        text = res.text
        match = re.search(r'content="(\{&quot;artist_has_more_songs.+?)"', text)
        if match:
            import json
            decoded = html_lib.unescape(match.group(1))
            data = json.loads(decoded)
            artist = data.get("artist", {})
            return artist.get("id"), artist.get("name")
    except Exception as e:
        logger.warning(f"Failed to scrape artist page: {e}")
    return None, None


@free_audit_router.get("/genius/catalog")
async def fetch_genius_catalog(
    url: str = Query(..., description="Genius artist URL"),
    limit: Optional[int] = Query(None, description="Max credited songs to return (for fast initial fetch)"),
):
    """
    Fetch all songs where an artist has writing/production credits from Genius.
    Enriches with Spotify ISRCs.
    """
    if not url:
        raise HTTPException(status_code=400, detail="url param required")

    headers = {"Authorization": f"Bearer {settings.genius_bearer_token}"}

    # 1. Resolve artist ID from URL
    artist_id = None
    artist_name = None

    slug_match = re.search(r'/artists/([^/?#]+)', url, re.IGNORECASE)
    if slug_match:
        slug = slug_match.group(1)
        if slug.isdigit():
            artist_id = int(slug)
        else:
            # Strategy 1: Genius artist search via Ionos proxy (bypasses Cloudflare)
            try:
                artist_search_res = await asyncio.to_thread(
                    lambda: requests.get(
                        "https://api-staging.verax.app/genius-proxy/search/artist",
                        params={"q": slug.replace("-", " ")},
                        timeout=10,
                    )
                )
                if artist_search_res.ok:
                    url_slug = slug.lower()
                    for section in artist_search_res.json().get("response", {}).get("sections", []):
                        for hit in section.get("hits", []):
                            result = hit.get("result", {})
                            a_url = result.get("url", "")
                            if a_url:
                                a_slug = a_url.rstrip("/").split("/")[-1].lower()
                                if a_slug == url_slug:
                                    artist_id = result.get("id")
                                    artist_name = result.get("name")
                                    break
                        if artist_id:
                            break
            except Exception as e:
                logger.warning(f"Genius artist search failed: {e}")

            # Strategy 2: Scrape Genius page HTML (works locally, may fail on cloud)
            if not artist_id:
                page_id, page_name = await asyncio.to_thread(_resolve_artist_from_page, url)
                if page_id:
                    artist_id = page_id
                    artist_name = page_name
                else:
                    artist_name = slug.replace("-", " ")

    # Strategy 3: Fallback song search API
    if artist_name and not artist_id:
        try:
            search_res = await asyncio.to_thread(
                _genius_get,
                f"https://api.genius.com/search",
                headers,
                {"q": artist_name},
            )
            if search_res.ok:
                hits = search_res.json().get("response", {}).get("hits", [])
                name_lower = artist_name.lower()

                for hit in hits:
                    for a in [hit.get("result", {}).get("primary_artist", {})] + (hit.get("result", {}).get("featured_artists") or []):
                        if a.get("name", "").lower() == name_lower:
                            artist_id = a.get("id")
                            artist_name = a.get("name")
                            break
                    if artist_id:
                        break
        except Exception as e:
            logger.warning(f"Genius search failed: {e}")

    # If we have ID but not name, fetch artist info
    if artist_id and not artist_name:
        try:
            art_res = await asyncio.to_thread(
                _genius_get,
                f"https://api.genius.com/artists/{artist_id}",
                headers,
            )
            if art_res.ok:
                artist_name = art_res.json().get("response", {}).get("artist", {}).get("name", f"Artist {artist_id}")
        except Exception:
            artist_name = f"Artist {artist_id}"

    if not artist_id:
        raise HTTPException(status_code=404, detail="Could not find artist. Check the URL.")

    logger.info(f"[FreeAudit/Genius] Fetching catalog for {artist_name} (id={artist_id})")

    # 2. Paginate through songs (cap at limit or 200 most popular)
    #    Use internal Genius API via Ionos proxy — it includes producer/writer credits,
    #    unlike the public API which only returns songs where artist is primary.
    PROXY_BASE = "https://api-staging.verax.app/genius-proxy"
    MAX_SONGS = limit if limit else 200
    all_song_stubs = []
    page = 1
    use_proxy = True

    while len(all_song_stubs) < MAX_SONGS:
        try:
            if use_proxy:
                songs_res = await asyncio.to_thread(
                    lambda p=page: requests.get(
                        f"{PROXY_BASE}/artists/{artist_id}/songs",
                        params={"page": p, "sort": "popularity"},
                        timeout=15,
                    )
                )
            else:
                songs_res = await asyncio.to_thread(
                    _genius_get,
                    f"https://api.genius.com/artists/{artist_id}/songs",
                    headers,
                    {"per_page": 50, "page": page, "sort": "popularity"},
                )
        except Exception as e:
            logger.warning(f"Genius songs page {page} failed: {e}")
            # If proxy failed on first page, fall back to public API
            if use_proxy and page == 1:
                use_proxy = False
                continue
            break

        if not songs_res.ok:
            if use_proxy and page == 1:
                use_proxy = False
                continue
            break

        songs_data = songs_res.json()
        page_songs = songs_data.get("response", {}).get("songs", [])
        if not page_songs:
            break

        for song in page_songs:
            song_id = song.get("id")
            if song_id:
                all_song_stubs.append(song)

        if len(all_song_stubs) >= MAX_SONGS:
            all_song_stubs = all_song_stubs[:MAX_SONGS]
            break

        next_page = songs_data.get("response", {}).get("next_page")
        if not next_page:
            break
        page += 1

    logger.info(f"[FreeAudit/Genius] Found {len(all_song_stubs)} song stubs, fetching details concurrently...")

    # Fetch song details concurrently (semaphore=15)
    genius_sem = asyncio.Semaphore(15)

    async def _fetch_song_detail_api(song_id, headers):
        """Fetch song detail from Genius API with cache."""
        cached = _get_cached_song_detail(song_id)
        if cached is not None:
            return cached

        detail = None
        # Try Genius API first
        try:
            detail_res = await asyncio.to_thread(
                _genius_get,
                f"https://api.genius.com/songs/{song_id}",
                headers,
                None,
                15,
            )
            if detail_res.ok:
                detail = detail_res.json().get("response", {}).get("song")
        except Exception:
            pass

        # Fallback: try proxy
        if not detail:
            try:
                proxy_res = await asyncio.to_thread(
                    lambda: requests.get(
                        f"{GENIUS_PROXY_BASE}/songs/{song_id}",
                        timeout=15,
                    )
                )
                if proxy_res.ok:
                    detail = proxy_res.json().get("response", {}).get("song")
            except Exception:
                pass

        if detail:
            _set_cached_song_detail(song_id, detail)
        return detail

    async def fetch_song_detail(song_stub):
        song_id = song_stub.get("id")
        async with genius_sem:
            detail = await _fetch_song_detail_api(song_id, headers)

            # If detail fetch failed (rate limit, etc.), build from stub data.
            # Genius returns these songs on the artist's page because they have
            # production/writing/performance credits — trust that and include all.
            if not detail:
                stub_primary = song_stub.get("primary_artist", {})
                is_primary = stub_primary.get("id") == artist_id

                title = song_stub.get("title", "Unknown Title")
                primary_artist_name = stub_primary.get("name") or song_stub.get("primary_artist_names", "Unknown Artist")

                release_date = None
                try:
                    rd = song_stub.get("release_date_for_display")
                    if rd:
                        from datetime import datetime
                        release_date = datetime.strptime(rd, "%B %d, %Y").strftime("%Y-%m-%d")
                except Exception:
                    pass

                pageviews = 0
                try:
                    pageviews = song_stub.get("stats", {}).get("pageviews", 0) or 0
                except Exception:
                    pass

                return {
                    "id": f"genius-{song_id}",
                    "geniusId": song_id,
                    "title": title,
                    "artist": primary_artist_name,
                    "album": "N/A",
                    "albumArt": song_stub.get("song_art_image_url"),
                    "isrc": "N/A",
                    "releaseDate": release_date or "2024-01-01",
                    "isProducer": not is_primary,
                    "isWriter": False,
                    "isPrimaryArtist": is_primary,
                    "hasCustomCredit": not is_primary,
                    "popularity": pageviews,
                    "source": "genius",
                }

            # Full detail available — check credits precisely
            is_producer = False
            is_writer = False
            has_custom_credit = False

            producers = detail.get("producer_artists") or []
            is_producer = any(p.get("id") == artist_id for p in producers)

            writers = detail.get("writer_artists") or []
            is_writer = any(w.get("id") == artist_id for w in writers)

            if not is_producer and not is_writer:
                for perf in (detail.get("custom_performances") or []):
                    if any(a.get("id") == artist_id for a in (perf.get("artists") or [])):
                        has_custom_credit = True
                        label = (perf.get("label") or "").lower()
                        if "produc" in label:
                            is_producer = True
                        if re.search(r'writ|compos', label):
                            is_writer = True
                        break

            is_primary = detail.get("primary_artist", {}).get("id") == artist_id

            if not is_producer and not is_writer and not has_custom_credit and not is_primary:
                return None

            primary_artist = detail.get("primary_artist", {}).get("name", "Unknown Artist")
            title = detail.get("title") or song_stub.get("title", "Unknown Title")

            release_date = None
            try:
                rd = detail.get("release_date_for_display")
                if rd:
                    from datetime import datetime
                    release_date = datetime.strptime(rd, "%B %d, %Y").strftime("%Y-%m-%d")
            except Exception:
                pass

            album_art = detail.get("song_art_image_url")
            album_data = detail.get("album")
            album = album_data.get("name", "N/A") if album_data else "N/A"

            # Use Genius pageviews as popularity metric
            pageviews = 0
            try:
                pageviews = detail.get("stats", {}).get("pageviews", 0) or 0
            except Exception:
                pass

            return {
                "id": f"genius-{song_id}",
                "geniusId": song_id,
                "title": title,
                "artist": primary_artist,
                "album": album,
                "albumArt": album_art,
                "isrc": "N/A",
                "releaseDate": release_date or "2024-01-01",
                "isProducer": is_producer,
                "isWriter": is_writer,
                "isPrimaryArtist": is_primary,
                "hasCustomCredit": has_custom_credit,
                "popularity": pageviews,
                "source": "genius",
            }

    detail_results = await asyncio.gather(*[fetch_song_detail(s) for s in all_song_stubs])
    songs = [r for r in detail_results if r is not None]
    songs.sort(key=lambda s: s.get("popularity", 0), reverse=True)

    logger.info(f"[FreeAudit/Genius] Found {len(songs)} credited songs for {artist_name}")

    # 3. Enrich with Spotify ISRCs (skip when limit is set for fast initial fetch)
    enriched = 0
    if not limit:
        try:
            spot_token = get_spotify_access_token()
            if spot_token:
                spot_headers = {"Authorization": f"Bearer {spot_token}"}
                spot_sem = asyncio.Semaphore(10)

                async def enrich_song(song):
                    async with spot_sem:
                        try:
                            q = re.sub(r'[()[\]]', '', f"{song['title']} {song['artist']}")
                            search_res = await asyncio.to_thread(
                                lambda q=q: requests.get(
                                    "https://api.spotify.com/v1/search",
                                    headers=spot_headers,
                                    params={"q": q, "type": "track", "limit": 5},
                                    timeout=10,
                                )
                            )
                            if not search_res.ok:
                                return

                            tracks = search_res.json().get("tracks", {}).get("items", [])
                            title_lower = re.sub(r'[^a-z0-9\s]', '', song["title"].lower())

                            best = None
                            for track in tracks:
                                track_title = re.sub(r'[^a-z0-9\s]', '', track["name"].lower())
                                if track_title == title_lower or track_title in title_lower or title_lower in track_title:
                                    best = track
                                    break
                            if not best and tracks:
                                best = tracks[0]

                            if best:
                                isrc = best.get("external_ids", {}).get("isrc")
                                if isrc:
                                    song["isrc"] = isrc
                                    song["spotifyId"] = best["id"]
                                    return True
                        except Exception:
                            pass
                        return False

                enrich_results = await asyncio.gather(*[enrich_song(s) for s in songs])
                enriched = sum(1 for r in enrich_results if r)
        except Exception as e:
            logger.warning(f"Spotify enrichment failed: {e}")
        logger.info(f"[FreeAudit/Genius] Enriched {enriched}/{len(songs)} with ISRCs")
    else:
        logger.info(f"[FreeAudit/Genius] Skipping ISRC enrichment for fast fetch (limit={limit})")
    return {"artistName": artist_name, "artistId": artist_id, "songs": songs, "total": len(songs)}


# ─── Spotify catalog fetch ─────────────────────────────────────────

@free_audit_router.get("/spotify/catalog")
async def fetch_spotify_catalog(
    url: str = Query(..., description="Spotify artist URL"),
    limit: Optional[int] = Query(None, description="Max songs to return (for fast initial fetch)"),
):
    """Fetch all albums and tracks for a Spotify artist."""
    if not url:
        raise HTTPException(status_code=400, detail="url param required")

    id_match = re.search(r'artist/([a-zA-Z0-9]+)', url)
    if not id_match:
        raise HTTPException(status_code=400, detail="Invalid Spotify artist URL")
    artist_id = id_match.group(1)

    token = get_spotify_access_token()
    if not token:
        raise HTTPException(status_code=500, detail="Failed to get Spotify token")

    auth_headers = {"Authorization": f"Bearer {token}"}

    # Get artist name
    try:
        artist_res = await asyncio.to_thread(
            lambda: requests.get(
                f"https://api.spotify.com/v1/artists/{artist_id}",
                headers=auth_headers,
                timeout=10,
            )
        )
    except Exception:
        raise HTTPException(status_code=500, detail="Failed to fetch artist")

    if not artist_res.ok:
        raise HTTPException(status_code=404, detail="Artist not found on Spotify")
    artist_name = artist_res.json().get("name", "Unknown")

    logger.info(f"[FreeAudit/Spotify] Fetching catalog for {artist_name} (id={artist_id}), limit={limit}")

    songs = []
    seen_ids = set()
    track_id_to_index = {}

    # ── Fast path: use top-tracks + first page of albums when limit is set ──
    if limit:
        # Start with Spotify top tracks (instant, up to 10 most popular)
        try:
            top_res = await asyncio.to_thread(
                lambda: requests.get(
                    f"https://api.spotify.com/v1/artists/{artist_id}/top-tracks",
                    headers=auth_headers,
                    timeout=10,
                )
            )
            if top_res.ok:
                for track in top_res.json().get("tracks", []):
                    if len(songs) >= limit:
                        break
                    if track["id"] in seen_ids:
                        continue
                    seen_ids.add(track["id"])
                    album = track.get("album") or {}
                    idx = len(songs)
                    track_id_to_index[track["id"]] = idx
                    songs.append({
                        "id": f"spotify-{track['id']}",
                        "spotifyId": track["id"],
                        "title": track["name"],
                        "artist": ", ".join(a["name"] for a in track.get("artists", [])) or artist_name,
                        "album": album.get("name", "N/A"),
                        "albumArt": (album.get("images") or [{}])[0].get("url") if album.get("images") else None,
                        "isrc": track.get("external_ids", {}).get("isrc", "N/A"),
                        "releaseDate": album.get("release_date"),
                        "popularity": track.get("popularity", 0),
                        "source": "spotify",
                    })
        except Exception as e:
            logger.warning(f"Spotify top-tracks failed: {e}")

        # Fill remaining from first page of albums only
        if len(songs) < limit:
            try:
                album_res = await asyncio.to_thread(
                    lambda: requests.get(
                        f"https://api.spotify.com/v1/artists/{artist_id}/albums?include_groups=album,single&limit=50",
                        headers=auth_headers,
                        timeout=10,
                    )
                )
                if album_res.ok:
                    albums = album_res.json().get("items", [])
                    album_sem = asyncio.Semaphore(10)

                    async def fetch_album_tracks_fast(alb):
                        found = []
                        async with album_sem:
                            try:
                                res = await asyncio.to_thread(
                                    lambda a=alb: requests.get(
                                        f"https://api.spotify.com/v1/albums/{a['id']}/tracks?limit=50",
                                        headers=auth_headers,
                                        timeout=10,
                                    )
                                )
                                if res.ok:
                                    for t in res.json().get("items", []):
                                        found.append((t, alb))
                            except Exception:
                                pass
                        return found

                    album_results = await asyncio.gather(*[fetch_album_tracks_fast(a) for a in albums])
                    for tracks_found in album_results:
                        for track, alb in tracks_found:
                            if len(songs) >= limit:
                                break
                            if track["id"] in seen_ids:
                                continue
                            seen_ids.add(track["id"])
                            idx = len(songs)
                            track_id_to_index[track["id"]] = idx
                            songs.append({
                                "id": f"spotify-{track['id']}",
                                "spotifyId": track["id"],
                                "title": track["name"],
                                "artist": ", ".join(a["name"] for a in track.get("artists", [])) or artist_name,
                                "album": alb.get("name", "N/A"),
                                "albumArt": (alb.get("images") or [{}])[0].get("url") if alb.get("images") else None,
                                "isrc": "N/A",
                                "releaseDate": alb.get("release_date"),
                                "popularity": 0,
                                "source": "spotify",
                            })
                        if len(songs) >= limit:
                            break
            except Exception as e:
                logger.warning(f"Spotify album fill failed: {e}")

        # Batch fetch ISRCs for songs that don't have them yet
        needs_isrc = [sid for sid, idx in track_id_to_index.items() if songs[idx]["isrc"] == "N/A"]
        if needs_isrc:
            batch_size = 50
            batches = [needs_isrc[i:i + batch_size] for i in range(0, len(needs_isrc), batch_size)]
            for batch in batches:
                try:
                    res = await asyncio.to_thread(
                        lambda ids=batch: requests.get(
                            "https://api.spotify.com/v1/tracks",
                            headers=auth_headers,
                            params={"ids": ",".join(ids)},
                            timeout=15,
                        )
                    )
                    if res.ok:
                        for full_track in (res.json().get("tracks") or []):
                            if not full_track:
                                continue
                            tid = full_track.get("id")
                            if tid and tid in track_id_to_index:
                                idx = track_id_to_index[tid]
                                songs[idx]["isrc"] = full_track.get("external_ids", {}).get("isrc", "N/A")
                                songs[idx]["popularity"] = full_track.get("popularity", 0)
                except Exception as e:
                    logger.warning(f"Spotify batch track fetch failed: {e}")

    # ── Full path: fetch all albums and tracks ──
    else:
        albums = []
        album_url = f"https://api.spotify.com/v1/artists/{artist_id}/albums?include_groups=album,single&limit=50"

        while album_url:
            try:
                album_res = await asyncio.to_thread(
                    lambda u=album_url: requests.get(u, headers=auth_headers, timeout=10)
                )
                if not album_res.ok:
                    break
                album_data = album_res.json()
                albums.extend(album_data.get("items", []))
                album_url = album_data.get("next")
            except Exception:
                break

        # Get tracks from all albums concurrently (semaphore=10)
        album_sem = asyncio.Semaphore(10)

        async def fetch_album_tracks(album):
            tracks_found = []
            async with album_sem:
                track_url = f"https://api.spotify.com/v1/albums/{album['id']}/tracks?limit=50"
                while track_url:
                    try:
                        track_res = await asyncio.to_thread(
                            lambda u=track_url: requests.get(u, headers=auth_headers, timeout=10)
                        )
                        if not track_res.ok:
                            break
                        track_data = track_res.json()
                        for track in track_data.get("items", []):
                            tracks_found.append((track, album))
                        track_url = track_data.get("next")
                    except Exception:
                        break
            return tracks_found

        album_results = await asyncio.gather(*[fetch_album_tracks(a) for a in albums])

        for tracks_found in album_results:
            for track, album in tracks_found:
                if track["id"] in seen_ids:
                    continue
                seen_ids.add(track["id"])
                idx = len(songs)
                track_id_to_index[track["id"]] = idx
                songs.append({
                    "id": f"spotify-{track['id']}",
                    "spotifyId": track["id"],
                    "title": track["name"],
                    "artist": ", ".join(a["name"] for a in track.get("artists", [])) or artist_name,
                    "album": album.get("name", "N/A"),
                    "albumArt": (album.get("images") or [{}])[0].get("url") if album.get("images") else None,
                    "isrc": "N/A",
                    "releaseDate": album.get("release_date"),
                    "popularity": 0,
                    "source": "spotify",
                })

        # Batch fetch full track data for ISRCs (50 at a time)
        all_track_ids = list(track_id_to_index.keys())
        batch_size = 50

        async def fetch_track_batch(ids_chunk):
            try:
                res = await asyncio.to_thread(
                    lambda ids=ids_chunk: requests.get(
                        "https://api.spotify.com/v1/tracks",
                        headers=auth_headers,
                        params={"ids": ",".join(ids)},
                        timeout=15,
                    )
                )
                if not res.ok:
                    return
                tracks = res.json().get("tracks", [])
                for full_track in tracks:
                    if not full_track:
                        continue
                    tid = full_track.get("id")
                    if tid and tid in track_id_to_index:
                        idx = track_id_to_index[tid]
                        songs[idx]["isrc"] = full_track.get("external_ids", {}).get("isrc", "N/A")
                        songs[idx]["popularity"] = full_track.get("popularity", 0)
            except Exception as e:
                logger.warning(f"Spotify batch track fetch failed: {e}")

        batches = [all_track_ids[i:i + batch_size] for i in range(0, len(all_track_ids), batch_size)]
        await asyncio.gather(*[fetch_track_batch(batch) for batch in batches])

    logger.info(f"[FreeAudit/Spotify] Found {len(songs)} songs for {artist_name}")
    return {"artistName": artist_name, "artistId": artist_id, "songs": songs, "total": len(songs)}


# ─── MLC audit endpoint ────────────────────────────────────────────

class AuditSong(BaseModel):
    id: str
    title: str
    artist: str
    albumArt: Optional[str] = None
    isrc: Optional[str] = None
    releaseDate: Optional[str] = None
    popularity: Optional[int] = 0
    source: Optional[str] = None


class AuditRequest(BaseModel):
    songs: List[AuditSong]
    ipNumber: Optional[str] = None
    publisherIpNumber: Optional[str] = None
    publisherName: Optional[str] = None
    writerName: Optional[str] = None


def ipi_matches(a: Optional[str], b: Optional[str]) -> bool:
    """Compare two IPI numbers with leading-zero tolerance."""
    if not a or not b:
        return False
    a_clean = str(a).strip()
    b_clean = str(b).strip()
    if a_clean == b_clean:
        return True
    return a_clean.lstrip("0") == b_clean.lstrip("0")


@free_audit_router.post("/mlc/audit")
async def run_mlc_audit(body: AuditRequest):
    """
    Audit songs against MLC database.
    Checks registration, writer IPI match, and publisher match.
    """
    if not body.songs:
        raise HTTPException(status_code=400, detail="songs array required")

    try:
        mlc = MLCClient(
            username=settings.mlc_username,
            password=settings.mlc_password,
            api_url=settings.mlc_api_url,
        )
    except Exception as e:
        logger.error(f"MLC client init failed: {e}")
        raise HTTPException(status_code=500, detail="MLC API configuration error")

    ip_number = body.ipNumber
    publisher_ip = body.publisherIpNumber
    publisher_name = body.publisherName
    writer_name = body.writerName

    logger.info(f"[FreeAudit/MLC] Auditing {len(body.songs)} songs, IPI={ip_number}")

    # Pre-authenticate so all parallel workers share the token
    try:
        await asyncio.to_thread(mlc.authenticate)
    except Exception as e:
        logger.warning(f"MLC pre-auth failed (will retry per-request): {e}")

    # Audit songs concurrently (semaphore=10)
    mlc_sem = asyncio.Semaphore(10)

    async def audit_single_song(song):
        song_result = {
            "id": song.id,
            "title": song.title,
            "artist": song.artist,
            "albumArt": song.albumArt,
            "isrc": song.isrc,
            "releaseDate": song.releaseDate,
            "popularity": song.popularity or 0,
            "source": song.source,
            "registered": False,
            "userMatched": False,
            "writerMatched": False,
            "publisherMatched": False,
            "matchedBy": None,
            "matchedWriterName": None,
            "mlcSongCode": None,
            "iswc": None,
            "writers": [],
            "publishers": [],
            "ipiNumbers": [],
            "issues": [],
            "warnings": [],
            "isrcMissing": False,
        }

        async with mlc_sem:
            try:
                mlc_data = None
                found_by_isrc = False

                # Strategy 1: Search by ISRC
                if song.isrc and song.isrc != "N/A":
                    try:
                        result = await asyncio.to_thread(mlc.get_complete_work_info_by_isrc, song.isrc)
                        if result and result.get("work"):
                            mlc_data = result
                            found_by_isrc = True
                    except Exception as e:
                        logger.warning(f"ISRC search failed for {song.isrc}: {e}")

                # Strategy 2: Search by artist + title
                if not mlc_data:
                    try:
                        recordings = await asyncio.to_thread(
                            mlc.search_recordings, song.artist, song.title
                        )
                        if recordings and isinstance(recordings, list) and len(recordings) > 0:
                            rec = recordings[0]
                            song_code = rec.get("mlcsongCode") or rec.get("mlcSongCode")
                            if song_code:
                                work = await asyncio.to_thread(mlc.get_work_by_id, song_code)
                                if work:
                                    mlc_data = {"recording": rec, "work": work, "mlc_song_code": song_code}
                    except Exception as e:
                        logger.warning(f"Title/artist search failed for {song.title}: {e}")

                # Strategy 3: Search works by title + writer IPI/name
                if not mlc_data and (ip_number or writer_name):
                    try:
                        works = await asyncio.to_thread(
                            mlc.search_by_title_and_writer,
                            song.title,
                            writer_name,
                            ip_number,
                        )
                        if works and isinstance(works, list) and len(works) > 0:
                            w = works[0]
                            song_code = w.get("mlcSongCode") or w.get("mlcsongCode")
                            if song_code:
                                work = await asyncio.to_thread(mlc.get_work_by_id, song_code)
                                if work:
                                    mlc_data = {"recording": None, "work": work, "mlc_song_code": song_code}
                    except Exception as e:
                        logger.warning(f"Works search failed for {song.title}: {e}")

                # Process results
                if mlc_data and mlc_data.get("work"):
                    work = mlc_data["work"]
                    song_result["registered"] = True
                    song_result["mlcSongCode"] = mlc_data.get("mlc_song_code")
                    song_result["iswc"] = work.get("iswc")

                    if not found_by_isrc and song.isrc and song.isrc != "N/A":
                        song_result["isrcMissing"] = True
                        song_result["issues"].append("ISRC not linked in registration - found via title/artist search")

                    # Extract writers and check IPI match
                    writer_matched = False
                    for wr in (work.get("writers") or []):
                        first = wr.get("writerFirstName") or ""
                        last = wr.get("writerLastName") or ""
                        full = f"{first} {last}".strip()
                        w_ipi = wr.get("writerIPI") or wr.get("ipiNumber") or wr.get("ipi")

                        if full:
                            song_result["writers"].append(full)
                        if w_ipi:
                            song_result["ipiNumbers"].append(w_ipi)

                        if ip_number and w_ipi and ipi_matches(ip_number, w_ipi):
                            writer_matched = True
                            song_result["writerMatched"] = True
                            song_result["userMatched"] = True
                            song_result["matchedBy"] = "ipi"
                            if full and not song_result["matchedWriterName"]:
                                song_result["matchedWriterName"] = full

                        if writer_name and full:
                            user_lower = writer_name.lower()
                            writer_lower = full.lower()
                            if user_lower in writer_lower or writer_lower in user_lower:
                                writer_matched = True
                                song_result["writerMatched"] = True
                                song_result["userMatched"] = True
                                if not song_result["matchedBy"]:
                                    song_result["matchedBy"] = "name"
                                if full and not song_result["matchedWriterName"]:
                                    song_result["matchedWriterName"] = full

                    # Extract publishers and check match
                    publisher_matched = False
                    all_pubs = []

                    def collect_pubs(pubs):
                        for p in pubs:
                            all_pubs.append(p)
                            if p.get("parentPublishers"):
                                collect_pubs(p["parentPublishers"])

                    collect_pubs(work.get("publishers") or [])

                    for pub in all_pubs:
                        p_name = pub.get("publisherName") or pub.get("name") or ""
                        p_ipi = pub.get("publisherIpiNumber") or pub.get("publisherIPI") or pub.get("ipiNumber")

                        if p_name:
                            song_result["publishers"].append(p_name)

                        if publisher_name and p_name and publisher_name.lower() == p_name.lower():
                            publisher_matched = True
                            song_result["publisherMatched"] = True
                            song_result["userMatched"] = True
                            if not song_result["matchedBy"]:
                                song_result["matchedBy"] = "name"

                        if publisher_ip and p_ipi and ipi_matches(publisher_ip, p_ipi):
                            publisher_matched = True
                            song_result["publisherMatched"] = True
                            song_result["userMatched"] = True
                            if not song_result["matchedBy"]:
                                song_result["matchedBy"] = "ipi"

                    # Issues for missing matches
                    if ip_number and not writer_matched:
                        song_result["issues"].append("Writer IPI not found on this work")
                    if publisher_ip and not publisher_matched:
                        song_result["issues"].append("Publisher IPI not found on this work")
                # Song not found — registered stays False (no need to add to issues)

                if not song.isrc or song.isrc == "N/A":
                    song_result["warnings"].append("No ISRC code available - may affect royalty matching")

            except Exception as e:
                song_result["issues"].append(f"Error checking this song: {str(e)}")

        return song_result

    results = await asyncio.gather(*[audit_single_song(song) for song in body.songs])

    # Sort by popularity (highest first)
    results = sorted(results, key=lambda s: s.get("popularity", 0), reverse=True)

    # Build summary
    registered = [s for s in results if s["registered"]]
    unregistered = [s for s in results if not s["registered"]]
    matched = [s for s in results if s["userMatched"]]
    with_issues = [s for s in results if s["registered"] and s["issues"]]
    missing_isrc = [s for s in results if s["isrcMissing"]]

    logger.info(f"[FreeAudit/MLC] Audit complete: {len(registered)}/{len(results)} registered, {len(matched)} matched")

    return {
        "songs": results,
        "summary": {
            "total": len(results),
            "registered": len(registered),
            "unregistered": len(unregistered),
            "userMatched": len(matched),
            "issueCount": len(with_issues),
            "isrcMissing": len(missing_isrc),
        },
    }


# ─── Send audit report via email ──────────────────────────────────

class ReportSong(BaseModel):
    title: str
    artist: str
    isrc: Optional[str] = None
    registered: bool = False
    issues: List[str] = []


class ReportSummary(BaseModel):
    total: int = 0
    registered: int = 0
    unregistered: int = 0
    issueCount: int = 0


class SendReportRequest(BaseModel):
    email: EmailStr
    artistName: str
    songs: List[ReportSong]
    summary: ReportSummary


@free_audit_router.post("/send-report")
async def send_audit_report(body: SendReportRequest):
    """Generate a PDF audit report and email it to the user."""
    try:
        pdf_gen = AuditReportPDFGenerator()
        pdf_buffer = pdf_gen.generate({
            "artistName": body.artistName,
            "songs": [s.model_dump() for s in body.songs],
            "summary": body.summary.model_dump(),
        })
        pdf_bytes = pdf_buffer.read()

        from email import encoders as _encoders
        from email.mime.base import MIMEBase
        from email.mime.multipart import MIMEMultipart
        from email.mime.text import MIMEText
        from smtplib import SMTP

        smtp = SMTP(settings.email_server, settings.email_port)
        smtp.starttls()
        smtp.ehlo()
        smtp.login(settings.email_username, settings.email_password)

        from_addr = f"Verax <{settings.email_username}>"
        to_addr = body.email

        msg = MIMEMultipart("mixed")
        msg["from"] = from_addr
        msg["to"] = to_addr
        msg["subject"] = f"Your Verax Catalog Audit Report — {body.artistName}"

        html_body = f"""
        <html>
        <body style="font-family: -apple-system, BlinkMacSystemFont, sans-serif; color: #333; max-width: 600px; margin: 0 auto; padding: 20px;">
            <h2 style="color: #111; margin-bottom: 4px;">Your Catalog Audit Report</h2>
            <p style="color: #666; margin-top: 0;">Results for <b>{body.artistName}</b></p>

            <div style="background: #f5f5f5; padding: 16px; border-radius: 8px; margin: 20px 0;">
                <p style="margin: 4px 0;"><b>{body.summary.total}</b> songs analyzed</p>
                <p style="margin: 4px 0; color: #22c55e;"><b>{body.summary.registered}</b> registered</p>
                <p style="margin: 4px 0; color: #ef4444;"><b>{body.summary.unregistered}</b> unregistered</p>
                <p style="margin: 4px 0; color: #f59e0b;"><b>{body.summary.issueCount}</b> with issues</p>
            </div>

            <p>Full details are in the attached PDF.</p>

            <hr style="border: none; border-top: 1px solid #eee; margin: 24px 0;">

            <h3 style="color: #111; margin-bottom: 8px;">Ready to fix these issues?</h3>

            <p><b>Self-Administer with Our Help</b><br>
            We'll help you set up your own publishing and guide you through registering your catalog — CWR filings, society registrations, and royalty tracking. You keep full ownership and control.</p>

            <p style="margin-left: 16px;">
                <a href="https://www.verax.app/signup" style="color: #6366f1; font-weight: 600;">Get started &rarr;</a>
            </p>

            <p><b>Flexible Administration</b><br>
            Prefer someone else to handle it? Our admin service takes care of registrations, royalty collection, and catalog management. Unlike standard admin deals: <b>no long-term commitment</b>, <b>no collection period</b>. Cancel anytime and we stop collecting immediately.</p>

            <p style="margin-left: 16px;">
                <a href="https://www.verax.app/publishing" style="color: #6366f1; font-weight: 600;">Learn about administration &rarr;</a>
            </p>

            <br>
            <p style="color: #999; font-size: 12px;">— Verax | verax.app</p>
        </body>
        </html>
        """
        msg.attach(MIMEText(html_body, "html"))

        attachment = MIMEBase("application", "pdf")
        attachment.set_payload(pdf_bytes)
        _encoders.encode_base64(attachment)
        filename = f"Verax_Audit_Report_{body.artistName.replace(' ', '_')}.pdf"
        attachment.add_header("Content-Disposition", f"attachment; filename={filename}")
        msg.attach(attachment)

        smtp.sendmail(from_addr, to_addr, msg.as_string())
        smtp.quit()

        logger.info(f"[FreeAudit] Sent report to {body.email} for {body.artistName}")
        return {"success": True}

    except Exception as e:
        import traceback
        logger.error(f"[FreeAudit] Failed to send report: {e}\n{traceback.format_exc()}")
        raise HTTPException(status_code=500, detail=f"Failed to send report: {str(e)}")
