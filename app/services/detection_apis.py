"""
API wrappers for music detection services
"""

import requests
import json
import time
from typing import Optional, Dict
from abc import ABC, abstractmethod
import structlog
from tenacity import retry, stop_after_attempt, wait_exponential

logger = structlog.get_logger()


class DetectionAPI(ABC):
    """
    Base class for detection APIs
    """

    def __init__(self, name: str):
        self.name = name
        self.call_count = 0
        self.error_count = 0
        self.total_time = 0

    @abstractmethod
    def recognize(self, audio_file: str) -> Optional[Dict]:
        """
        Recognize audio file and return standardized result
        """
        pass

    def get_stats(self) -> Dict:
        """
        Get API usage statistics
        """
        return {
            "engine": self.name,
            "calls": self.call_count,
            "errors": self.error_count,
            "avg_time": self.total_time / self.call_count if self.call_count > 0 else 0,
        }


class AuddAPI(DetectionAPI):
    """
    Audd.io API wrapper
    https://audd.io/
    """

    def __init__(self, api_token: str):
        super().__init__("audd")
        self.api_token = api_token
        self.base_url = "https://api.audd.io/"

    @retry(
        stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10)
    )
    def recognize(self, audio_file: str) -> Optional[Dict]:
        """
        Recognize audio file using Audd.io
        """
        start_time = time.time()
        self.call_count += 1

        try:
            with open(audio_file, "rb") as f:
                audio_data = f.read()

            files = {"file": audio_data}
            data = {"api_token": self.api_token, "return": "apple_music,spotify"}

            response = requests.post(self.base_url, data=data, files=files, timeout=30)
            response.raise_for_status()
            result = response.json()

            elapsed = time.time() - start_time
            self.total_time += elapsed

            if result.get("status") == "success" and result.get("result"):
                res = result["result"]

                # Extract metadata
                artist = res.get("artist", "Unknown")
                title = res.get("title", "Unknown")
                album = res.get("album")
                release_date = res.get("release_date")
                label = res.get("label")

                # Get streaming IDs
                spotify_id = None
                apple_music_id = None

                if "spotify" in res:
                    spotify_id = res["spotify"].get("id")

                if "apple_music" in res:
                    apple_music_id = res["apple_music"].get("id")

                logger.info("audd_match", artist=artist, title=title, time=elapsed)

                return {
                    "engine": "audd",
                    "sample_name": f"{artist} - {title}",
                    "artist": artist,
                    "title": title,
                    "album": album,
                    "release_date": release_date,
                    "label": label,
                    "spotify_id": spotify_id,
                    "apple_music_id": apple_music_id,
                    "confidence": 85,  # Audd doesn't return confidence
                    "processing_time": elapsed,
                    "raw_response": result,
                }

            logger.debug("audd_no_match", time=elapsed)
            return None

        except requests.exceptions.RequestException as e:
            self.error_count += 1
            logger.error("audd_api_error", error=str(e))
            return None

        except Exception as e:
            self.error_count += 1
            logger.error("audd_unexpected_error", error=str(e))
            return None


class ACRCloudAPI(DetectionAPI):
    """
    ACRCloud API wrapper
    https://www.acrcloud.com/
    """

    def __init__(self, access_key: str, access_secret: str, host: str):
        super().__init__("acrcloud")
        self.access_key = access_key
        self.access_secret = access_secret
        self.host = host

    @retry(
        stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10)
    )
    def recognize(self, audio_file: str) -> Optional[Dict]:
        """
        Recognize audio file using ACRCloud
        """
        start_time = time.time()
        self.call_count += 1

        try:
            from acrcloud.recognizer import ACRCloudRecognizer

            config = {
                "host": self.host,
                "access_key": self.access_key,
                "access_secret": self.access_secret,
                "timeout": 10,
            }

            recognizer = ACRCloudRecognizer(config)
            result = recognizer.recognize_by_file(audio_file, 0)
            result_dict = json.loads(result)

            elapsed = time.time() - start_time
            self.total_time += elapsed

            if result_dict["status"]["code"] == 0:
                music = result_dict["metadata"]["music"][0]

                # Extract metadata
                artist = music["artists"][0]["name"]
                title = music["title"]
                album = music.get("album", {}).get("name")
                release_date = music.get("release_date")
                label = music.get("label")
                duration_ms = music.get("duration_ms")
                score = music.get("score", 0)  # 0-100

                # Get external IDs
                spotify_id = None
                isrc = None

                if "external_metadata" in music:
                    ext = music["external_metadata"]
                    if "spotify" in ext:
                        spotify_id = ext["spotify"].get("track", {}).get("id")

                if "external_ids" in music:
                    isrc = music["external_ids"].get("isrc")

                logger.info(
                    "acrcloud_match",
                    artist=artist,
                    title=title,
                    score=score,
                    time=elapsed,
                )

                return {
                    "engine": "acrcloud",
                    "sample_name": f"{artist} - {title}",
                    "artist": artist,
                    "title": title,
                    "album": album,
                    "release_date": release_date,
                    "label": label,
                    "isrc": isrc,
                    "spotify_id": spotify_id,
                    "duration_ms": duration_ms,
                    "confidence": score,
                    "processing_time": elapsed,
                    "raw_response": result_dict,
                }

            logger.debug("acrcloud_no_match", time=elapsed)
            return None

        except Exception as e:
            self.error_count += 1
            logger.error("acrcloud_error", error=str(e))
            return None


class AcoustIDAPI(DetectionAPI):
    """
    AcoustID API wrapper (free)
    https://acoustid.org/
    """

    def __init__(self, api_key: str):
        super().__init__("acoustid")
        self.api_key = api_key
        self.base_url = "https://api.acoustid.org/v2/lookup"

    @retry(
        stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10)
    )
    def recognize(self, audio_file: str) -> Optional[Dict]:
        """
        Recognize audio file using AcoustID + MusicBrainz
        """
        start_time = time.time()
        self.call_count += 1

        try:
            import acoustid
            import os
            import platform
            from pathlib import Path

            # Set fpcalc path - use local binary from repo
            # Get the project root (4 levels up from this file)
            project_root = Path(__file__).parent.parent.parent

            # Detect platform and set appropriate fpcalc path
            system = platform.system().lower()
            if system == "windows":
                fpcalc_path = project_root / "bin" / "chromaprint" / "windows" / "fpcalc.exe"
            else:  # Linux, macOS, etc.
                fpcalc_path = project_root / "bin" / "chromaprint" / "linux" / "fpcalc"

            # Set the fpcalc command for acoustid
            if fpcalc_path.exists():
                acoustid.FPCALC_COMMAND = str(fpcalc_path)
                logger.debug("acoustid_fpcalc_path", path=str(fpcalc_path))
            else:
                logger.warning("acoustid_fpcalc_not_found", path=str(fpcalc_path))

            # Get fingerprint
            duration, fingerprint = acoustid.fingerprint_file(audio_file)

            # Query API
            params = {
                "client": self.api_key,
                "duration": int(duration),
                "fingerprint": fingerprint,
                "meta": "recordings releasegroups compress",
            }

            response = requests.get(self.base_url, params=params, timeout=30)
            response.raise_for_status()
            result = response.json()

            elapsed = time.time() - start_time
            self.total_time += elapsed

            if result.get("status") == "ok" and result.get("results"):
                # Get best match (highest score)
                best_match = result["results"][0]

                if "recordings" in best_match and best_match["recordings"]:
                    recording = best_match["recordings"][0]

                    # Extract metadata
                    title = recording.get("title", "Unknown")
                    artist = "Unknown"

                    if "artists" in recording and recording["artists"]:
                        artist = recording["artists"][0].get("name", "Unknown")

                    # Get release info
                    album = None
                    release_date = None

                    if "releasegroups" in recording and recording["releasegroups"]:
                        release_group = recording["releasegroups"][0]
                        album = release_group.get("title")

                        if "releases" in release_group and release_group["releases"]:
                            release = release_group["releases"][0]
                            release_date = release.get("date", {}).get("year")

                    # AcoustID score is 0-1, convert to 0-100
                    score = best_match.get("score", 0) * 100

                    # Get MusicBrainz ID
                    mb_recording_id = recording.get("id")

                    logger.info(
                        "acoustid_match",
                        artist=artist,
                        title=title,
                        score=score,
                        time=elapsed,
                    )

                    return {
                        "engine": "acoustid",
                        "sample_name": f"{artist} - {title}",
                        "artist": artist,
                        "title": title,
                        "album": album,
                        "release_date": release_date,
                        "musicbrainz_id": mb_recording_id,
                        "confidence": score,
                        "processing_time": elapsed,
                        "raw_response": result,
                    }

            logger.debug("acoustid_no_match", time=elapsed)
            return None

        except Exception as e:
            self.error_count += 1
            logger.error("acoustid_error", error=str(e))
            return None


class DetectionAPIFactory:
    """
    Factory for creating detection API instances
    """

    @staticmethod
    def create_audd(api_token: str) -> AuddAPI:
        return AuddAPI(api_token)

    @staticmethod
    def create_acrcloud(access_key: str, access_secret: str, host: str) -> ACRCloudAPI:
        return ACRCloudAPI(access_key, access_secret, host)

    @staticmethod
    def create_acoustid(api_key: str) -> AcoustIDAPI:
        return AcoustIDAPI(api_key)

    @staticmethod
    def create_all(config: Dict) -> Dict[str, DetectionAPI]:
        """
        Create all API instances from config
        """
        apis = {}

        if config.get("AUDD_API_KEY"):
            apis["audd"] = DetectionAPIFactory.create_audd(config["AUDD_API_KEY"])

        if all(
            [
                config.get("ACRCLOUD_KEY"),
                config.get("ACRCLOUD_SECRET"),
                config.get("ACRCLOUD_HOST"),
            ]
        ):
            apis["acrcloud"] = DetectionAPIFactory.create_acrcloud(
                config["ACRCLOUD_KEY"],
                config["ACRCLOUD_SECRET"],
                config["ACRCLOUD_HOST"],
            )

        if config.get("ACOUSTID_KEY"):
            apis["acoustid"] = DetectionAPIFactory.create_acoustid(
                config["ACOUSTID_KEY"]
            )

        return apis
