"""
Writer-to-Artist Resolution Service

Resolves performing artist names from songwriter/composer names using
a multi-API chain: MLC recordings → Spotify → title-only fallback.

Writers/composers are NOT the same as performing artists. When a royalty
statement only provides a writer name, this service finds the actual
recording artist.
"""

import os
import requests
import logging
from typing import Optional, Dict, Any

logger = logging.getLogger(__name__)


def resolve_artist_from_writer(
    title: str,
    writer: str,
    isrc: Optional[str] = None,
    spotify_access_token: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Resolve performing artist from writer name + song title.

    Resolution chain:
    1. If ISRC available → MLC recording lookup → get performing artist
    2. If writer + title → MLC search_by_title_and_writer → find work → find recording artist
    3. Fallback → MLC recording search by title only → get performing artist

    Args:
        title: Song title
        writer: Writer/composer/songwriter name
        isrc: ISRC code (optional, most reliable)
        spotify_access_token: Spotify token for fallback resolution

    Returns:
        Dict with resolved artist info, or empty dict if unresolved:
        {
            "artist": "Performing Artist Name",
            "isrc": "USRC...",
            "source": "mlc_isrc" | "mlc_writer" | "mlc_title" | "spotify" | None
        }
    """
    if not title:
        return {}

    result = {}

    # Try MLC-based resolution
    try:
        mlc_result = _resolve_via_mlc(title, writer, isrc)
        if mlc_result:
            return mlc_result
    except Exception as e:
        logger.warning(f"MLC resolution failed for '{title}' / writer '{writer}': {e}")

    # Fallback: Spotify title-only search (don't use writer as artist)
    if spotify_access_token:
        try:
            spotify_result = _resolve_via_spotify_title(title, spotify_access_token)
            if spotify_result:
                return spotify_result
        except Exception as e:
            logger.warning(f"Spotify resolution failed for '{title}': {e}")

    return result


def _resolve_via_mlc(
    title: str,
    writer: Optional[str] = None,
    isrc: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """
    Resolve artist via MLC API.

    Strategy:
    1. ISRC → search_recordings_by_isrc → get artist from recording
    2. Writer + title → search_by_title_and_writer → get work → search recordings by title
    3. Title only → search_recordings by title → get artist
    """
    from app.libs.MLC.mlc_api import MLCClient

    try:
        mlc = MLCClient()
    except (ValueError, Exception) as e:
        logger.info(f"MLC client not available: {e}")
        return None

    # Strategy 1: ISRC lookup (most reliable)
    if isrc:
        try:
            recordings = mlc.search_recordings_by_isrc(isrc)
            if recordings and isinstance(recordings, list) and len(recordings) > 0:
                recording = recordings[0]
                artist = recording.get("artist", "")
                if artist:
                    logger.info(
                        f"MLC ISRC resolution: '{title}' → artist '{artist}' (ISRC: {isrc})"
                    )
                    return {
                        "artist": artist,
                        "isrc": isrc,
                        "source": "mlc_isrc",
                    }
        except Exception as e:
            logger.warning(f"MLC ISRC lookup failed for {isrc}: {e}")

    # Strategy 2: Writer + title → find work → find recording
    if writer and title:
        try:
            works = mlc.search_by_title_and_writer(title=title, writer_name=writer)
            if works and isinstance(works, list) and len(works) > 0:
                # Try to find recordings for the matched work
                for work in works[:3]:  # Check top 3 matches
                    work_title = work.get("primaryTitle", "")
                    # Verify title similarity
                    if not _titles_match(title, work_title):
                        continue

                    # Search recordings by title to find performing artist
                    try:
                        recordings = mlc.search_recordings(title=title)
                        if recordings and isinstance(recordings, list):
                            for rec in recordings[:5]:
                                rec_artist = rec.get("artist", "")
                                if rec_artist:
                                    logger.info(
                                        f"MLC writer resolution: '{title}' by writer '{writer}' → artist '{rec_artist}'"
                                    )
                                    return {
                                        "artist": rec_artist,
                                        "isrc": rec.get("isrc", isrc),
                                        "source": "mlc_writer",
                                    }
                    except Exception as e:
                        logger.warning(f"MLC recording search failed for '{title}': {e}")
        except Exception as e:
            logger.warning(f"MLC writer search failed for '{title}' / '{writer}': {e}")

    # Strategy 3: Title-only recording search
    if title:
        try:
            recordings = mlc.search_recordings(title=title)
            if recordings and isinstance(recordings, list):
                for rec in recordings[:5]:
                    rec_title = rec.get("title", "")
                    rec_artist = rec.get("artist", "")
                    if rec_artist and _titles_match(title, rec_title):
                        logger.info(
                            f"MLC title resolution: '{title}' → artist '{rec_artist}'"
                        )
                        return {
                            "artist": rec_artist,
                            "isrc": rec.get("isrc", isrc),
                            "source": "mlc_title",
                        }
        except Exception as e:
            logger.warning(f"MLC title search failed for '{title}': {e}")

    return None


def _resolve_via_spotify_title(
    title: str,
    access_token: str,
) -> Optional[Dict[str, Any]]:
    """
    Resolve artist via Spotify title-only search.
    Does NOT use the writer name — Spotify indexes by performing artist.
    """
    import re

    search_url = "https://api.spotify.com/v1/search"
    headers = {"Authorization": f"Bearer {access_token}"}
    params = {"q": f'track:"{title}"', "type": "track", "limit": 10}

    resp = requests.get(search_url, headers=headers, params=params)
    if not resp.ok:
        return None

    tracks = resp.json().get("tracks", {}).get("items", [])
    if not tracks:
        return None

    def normalize(t):
        return re.sub(r"[^a-z0-9\s]", "", t.lower()).strip()

    title_normalized = normalize(title)

    for track in tracks:
        track_title = normalize(track.get("name", ""))
        if track_title == title_normalized:
            artists = track.get("artists", [])
            if artists:
                artist_name = artists[0].get("name", "")
                ext_ids = track.get("external_ids", {})
                logger.info(
                    f"Spotify title resolution: '{title}' → artist '{artist_name}'"
                )
                return {
                    "artist": artist_name,
                    "isrc": ext_ids.get("isrc"),
                    "source": "spotify",
                }

    return None


def _titles_match(title1: str, title2: str) -> bool:
    """Case-insensitive title comparison with normalization."""
    import re

    def normalize(t):
        return re.sub(r"[^a-z0-9\s]", "", t.lower()).strip()

    return normalize(title1) == normalize(title2)
