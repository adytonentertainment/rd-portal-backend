import httpx
from typing import List, Dict, Optional, Any
from datetime import date
from .schemas import HistoricStatsResponse

from app.settings import get_settings
from app.logger import get_logger

logger = get_logger("Songstats API")

# Supported streaming services by Songstats API
SUPPORTED_SOURCES = [
    "spotify",
    "apple_music",
    "youtube",
    "tiktok",
    "instagram",
    "amazon_music",
    "deezer",
    "shazam",
    "soundcloud",
    "tidal",
    "facebook",
    "twitter"
]

class SongstatsAPI:
    """Client for Songstats API to fetch playcount and streaming data"""

    def __init__(self):
        self.settings = get_settings()
        self.api_key = self.settings.songstats_api_key
        self.base_url = "https://api.songstats.com/enterprise/v1"
        self.timeout = 30.0

        if not self.api_key:
            logger.error("Songstats API key not configured.")

    async def get_track_playcount_history(
        self,
        spotify_track_id: str,
        start_date: Optional[date] = None,
        end_date: Optional[date] = None,
        sources: List[str] = None
    ) -> HistoricStatsResponse:
        if not self.api_key:
            raise ValueError("Songstats API key not configured")

        url = f"{self.base_url}/tracks/historic_stats"
        params = {
            "spotify_track_id": spotify_track_id,
            "source": ",".join(sources) if sources else "spotify",
            "with_aggregates": "false"
        }

        if start_date:
            params["start_date"] = start_date.isoformat()
        if end_date:
            params["end_date"] = end_date.isoformat()

        headers = {
            "apikey": self.api_key,
            "Content-Type": "application/json"
        }

        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.get(url, params=params, headers=headers)

                if response.status_code == 200:
                    data = response.json()
                    logger.info(f"Successfully fetched Songstats data for track {spotify_track_id} from {start_date} to {end_date}")
                    return HistoricStatsResponse(**data)
                else:
                    logger.error(f"Songstats API error {response.status_code}: {response.text}")
                    raise ValueError(f"Songstats API returned status {response.status_code}: {response.text}")

        except httpx.HTTPError as e:
            logger.error(f"HTTP error fetching Songstats data for {spotify_track_id}: {e}")
            raise
        except Exception as e:
            logger.error(f"Error fetching Songstats data for {spotify_track_id}: {e}")
            raise

    async def get_multiple_tracks_playcount(
        self,
        spotify_track_ids: List[str],
        target_date: Optional[date] = None
    ) -> Dict[str, Any]:
        """
        Fetch playcount data for multiple tracks at once

        Args:
            spotify_track_ids: List of Spotify track IDs
            target_date: Specific date to fetch data for (defaults to today)

        Returns:
            Dict mapping track_id to playcount data
        """
        if not self.api_key:
            raise ValueError("Songstats API key not configured")

        url = f"{self.base_url}/tracks/batch_stats"
        params = {
            "spotify_track_ids": ",".join(spotify_track_ids),
            "source": "spotify"
        }

        if target_date:
            params["date"] = target_date.isoformat()

        headers = {
            "apikey": self.api_key,
            "Content-Type": "application/json"
        }

        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.get(url, params=params, headers=headers)

                if response.status_code == 200:
                    data = response.json()
                    logger.info(f"Successfully fetched Songstats data for {len(spotify_track_ids)} tracks")
                    return data
                elif response.status_code == 404:
                    # Batch endpoint doesn't exist, fall back to individual requests
                    logger.warning("Batch endpoint not available, falling back to individual track requests")
                    return await self._fetch_tracks_individually(spotify_track_ids, target_date)
                else:
                    logger.error(f"Songstats API error {response.status_code}: {response.text}")
                    raise ValueError(f"Songstats API returned status {response.status_code}: {response.text}")

        except httpx.HTTPError as e:
            logger.error(f"HTTP error fetching Songstats data for multiple tracks: {e}")
            raise
        except Exception as e:
            logger.error(f"Error fetching Songstats data for multiple tracks: {e}")
            raise

    async def get_track_info(
        self,
        spotify_track_id: str,
    ) -> Dict[str, Any]:
        """
        Fetch track info for a single track

        Args:
            spotify_track_id: Spotify track ID

        Returns:
            Dict containing track info
        """
        if not self.api_key:
            raise ValueError("Songstats API key not configured")

        url = f"{self.base_url}/tracks/info"
        params = {
            "spotify_track_id": spotify_track_id,
        }

        headers = {
            "apikey": self.api_key,
            "Content-Type": "application/json"
        }

        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.get(url, params=params, headers=headers)

                if response.status_code == 200:
                    data = response.json()
                    logger.info(f"Successfully fetched Songstats track info for track {spotify_track_id}")
                    return data
                else:
                    logger.error(f"Songstats API error {response.status_code}: {response.text}")
                    raise ValueError(f"Songstats API returned status {response.status_code}: {response.text}")

        except httpx.HTTPError as e:
            logger.error(f"HTTP error fetching Songstats track info for {spotify_track_id}: {e}")
            raise
        except Exception as e:
            logger.error(f"Error fetching Songstats track info for {spotify_track_id}: {e}")
            raise