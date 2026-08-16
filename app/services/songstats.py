"""
Songstats API Integration Service

This module provides functions to interact with the Songstats API
to fetch streaming data from multiple platforms (Spotify, Apple Music, YouTube, etc.)
"""

import requests
from typing import Dict, List, Optional
from datetime import datetime, timedelta
import logging

logger = logging.getLogger(__name__)


class SongstatsAPI:
    """Songstats API Client"""

    BASE_URL = "https://api.songstats.com/enterprise/v1"

    def __init__(self, api_key: str):
        """
        Initialize Songstats API client

        Args:
            api_key: Songstats API key
        """
        self.api_key = api_key
        self.headers = {"apikey": api_key, "Accept": "application/json"}

    def search_track(self, query: str, limit: int = 10) -> Optional[Dict]:
        """
        Search for tracks by name/artist

        Args:
            query: Search query (track name and/or artist)
            limit: Maximum number of results to return

        Returns:
            Search results with track information
        """
        try:
            url = f"{self.BASE_URL}/tracks/search"
            params = {"q": query, "limit": limit}
            response = requests.get(
                url, headers=self.headers, params=params, timeout=10
            )
            response.raise_for_status()
            return response.json()
        except requests.exceptions.RequestException as e:
            logger.error(f"Error searching for track '{query}': {e}")
            return None

    def get_track_by_isrc(
        self, isrc: str, title: str = None, artist: str = None, spotify_track_id: str = None
    ) -> Optional[Dict]:
        """
        Get track information by ISRC

        The Songstats API doesn't support direct ISRC lookup, so we search by Spotify ID first
        (most accurate), then fallback to title + artist.

        Args:
            isrc: International Standard Recording Code
            title: Track title (optional but recommended for better matching)
            artist: Artist name (optional but recommended for better matching)
            spotify_track_id: Spotify track ID (optional but most accurate)

        Returns:
            Track information including Songstats track ID
        """
        try:
            # Use search endpoint
            url = f"{self.BASE_URL}/tracks/search"

            results = []

            # Try 1: Search by Spotify track ID (MOST ACCURATE)
            if spotify_track_id:
                params = {"q": spotify_track_id, "limit": 5}
                logger.info(f"Searching SongStats by Spotify ID: {spotify_track_id}")
                response = requests.get(
                    url, headers=self.headers, params=params, timeout=10
                )
                response.raise_for_status()
                data = response.json()
                results = data.get("results", [])

                if results:
                    logger.info(f"✅ Found exact match via Spotify ID for {title} - {artist}")
                    return results[0]

            # Try 2: Search by title + artist (more specific than title alone)
            if not results and title and artist:
                # Take only first artist if multiple
                first_artist = artist.split(',')[0].strip()
                search_query = f"{title} {first_artist}"
                params = {"q": search_query, "limit": 10}
                logger.info(f"Searching SongStats with: {search_query}")
                response = requests.get(
                    url, headers=self.headers, params=params, timeout=10
                )
                response.raise_for_status()
                data = response.json()
                results = data.get("results", [])

            # Try 3: Search by title only
            if not results and title:
                params = {"q": title, "limit": 10}
                logger.info(f"Searching SongStats for title: {title}")
                response = requests.get(
                    url, headers=self.headers, params=params, timeout=10
                )
                response.raise_for_status()
                data = response.json()
                results = data.get("results", [])

            # Try 4: If still no results, try with ISRC
            if not results:
                params = {"q": isrc, "limit": 10}
                logger.info(f"Final attempt - searching SongStats by ISRC: {isrc}")
                response = requests.get(
                    url, headers=self.headers, params=params, timeout=10
                )
                response.raise_for_status()
                data = response.json()
                results = data.get("results", [])

            if not results:
                logger.warning(
                    f"No search results found for track: {title} - {artist} (ISRC: {isrc})"
                )
                return None

            # Return the best match
            logger.info(f"Found Songstats track for {title} - {artist}")
            return results[0]

        except requests.exceptions.RequestException as e:
            logger.error(f"Error fetching track by ISRC {isrc}: {e}")
            return None

    def get_track_stats(
        self, track_id: str, start_date: str = None, end_date: str = None
    ) -> Optional[Dict]:
        """
        Get streaming statistics for a track

        Args:
            track_id: Songstats track ID
            start_date: Start date in YYYY-MM-DD format (optional)
            end_date: End date in YYYY-MM-DD format (optional)

        Returns:
            Streaming statistics including Spotify, Apple Music, YouTube, etc.
        """
        try:
            # Use the historic_stats endpoint which passes track_id as a parameter
            url = f"{self.BASE_URL}/tracks/historic_stats"
            params = {"songstats_track_id": track_id}

            if start_date:
                params["start_date"] = start_date
            if end_date:
                params["end_date"] = end_date

            response = requests.get(
                url, headers=self.headers, params=params, timeout=30
            )
            response.raise_for_status()

            data = response.json()

            # The historic_stats endpoint returns data in stats array
            # Structure: {stats: [{source: 'spotify', data: {history: [...]}}, ...]}
            result = {}

            stats_array = data.get("stats", [])

            # Add type checking and better error handling
            if not isinstance(stats_array, list):
                logger.error(
                    f"Unexpected stats format: expected list, got {type(stats_array)}"
                )
                return {}

            logger.info(
                f"Processing {len(stats_array)} stat sources for track {track_id}"
            )

            for stat in stats_array:
                if not isinstance(stat, dict):
                    logger.warning(f"Skipping invalid stat entry: {type(stat)}")
                    continue

                source = stat.get("source")
                data_obj = stat.get("data", {})

                if not isinstance(data_obj, dict):
                    logger.warning(
                        f"Invalid data object for source {source}: {type(data_obj)}"
                    )
                    continue

                history = data_obj.get("history", [])

                if not history or len(history) == 0:
                    continue

                # Get the latest data point
                latest = history[-1]

                if not isinstance(latest, dict):
                    logger.warning(
                        f"Invalid latest data point for {source}: {type(latest)}"
                    )
                    continue

                if source == "spotify":
                    streams = latest.get("streams_total", 0)
                    result["spotify"] = {"streams": streams}
                    logger.info(f"Found Spotify streams: {streams}")
                elif source == "apple_music":
                    plays = latest.get("plays_total", 0)
                    result["apple_music"] = {"plays": plays}
                    logger.info(f"Found Apple Music plays: {plays}")
                elif source == "youtube":
                    views = latest.get("video_views_total", 0)
                    result["youtube"] = {"views": views}
                    logger.info(f"Found YouTube views: {views}")
                elif source == "deezer":
                    result["deezer"] = {"listeners": latest.get("listeners_total", 0)}
                elif source == "amazon_music":
                    result["amazon_music"] = {"streams": latest.get("streams_total", 0)}
                elif source == "tiktok":
                    result["tiktok"] = {
                        "video_views": latest.get("video_views_total", 0)
                    }
                elif source == "shazam":
                    result["shazam"] = {"count": latest.get("shazams_total", 0)}

            logger.info(f"Returning stats result: {result}")
            return result

        except requests.exceptions.RequestException as e:
            logger.error(f"Error fetching track stats for {track_id}: {e}")
            # Return empty dict instead of None to avoid breaking downstream code
            return {
                "error": str(e),
                "track_id": track_id,
                "message": "Stats endpoint unavailable. This may be a limitation of the API key tier.",
            }

    def get_spotify_streams(
        self, isrc: str, title: str = None, artist: str = None
    ) -> Optional[int]:
        """
        Get current Spotify stream count for a track

        Args:
            isrc: International Standard Recording Code
            title: Track title (optional but recommended)
            artist: Artist name (optional but recommended)

        Returns:
            Spotify stream count or None if not found
        """
        track_info = self.get_track_by_isrc(isrc, title, artist)
        if not track_info:
            return None

        track_id = track_info.get("songstats_track_id") or track_info.get("id")
        if not track_id:
            return None

        stats = self.get_track_stats(track_id)
        if not stats:
            return None

        # Extract Spotify streams from stats
        spotify_data = stats.get("spotify", {})
        return spotify_data.get("streams")

    def get_apple_music_streams(
        self, isrc: str, title: str = None, artist: str = None
    ) -> Optional[int]:
        """
        Get current Apple Music stream count for a track

        Args:
            isrc: International Standard Recording Code
            title: Track title (optional but recommended)
            artist: Artist name (optional but recommended)

        Returns:
            Apple Music stream count or None if not found
        """
        track_info = self.get_track_by_isrc(isrc, title, artist)
        if not track_info:
            return None

        track_id = track_info.get("songstats_track_id") or track_info.get("id")
        if not track_id:
            return None

        stats = self.get_track_stats(track_id)
        if not stats:
            return None

        # Extract Apple Music streams from stats
        apple_music_data = stats.get("apple_music", {})
        return apple_music_data.get("plays")

    def get_all_platform_streams(
        self, isrc: str, title: str = None, artist: str = None
    ) -> Dict[str, Optional[int]]:
        """
        Get stream counts from all available platforms for a track

        Args:
            isrc: International Standard Recording Code
            title: Track title (optional but recommended for better matching)
            artist: Artist name (optional but recommended for better matching)

        Returns:
            Dictionary with platform names as keys and stream counts as values
            Example: {'spotify': 1000000, 'apple_music': 500000, 'youtube': 2000000}
        """
        track_info = self.get_track_by_isrc(isrc, title, artist)
        if not track_info:
            logger.warning(f"Track not found for: {title} - {artist} (ISRC: {isrc})")
            return {}

        track_id = track_info.get("songstats_track_id") or track_info.get("id")
        if not track_id:
            logger.warning(f"No track ID found for: {title} - {artist} (ISRC: {isrc})")
            return {}

        stats = self.get_track_stats(track_id)
        if not stats:
            logger.warning(f"No stats found for track ID: {track_id}")
            return {}

        # stats already has the correct format from get_track_stats
        # Format: {'spotify': {'streams': 123}, 'apple_music': {'plays': 456}, ...}
        logger.info(f"Fetched platform streams for {title} - {artist}: {stats}")
        return stats
