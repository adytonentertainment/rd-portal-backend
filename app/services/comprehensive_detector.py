"""
Comprehensive Beat Detection Service
Multi-strategy detection with high confidence requirements
"""

import os
import time
import tempfile
import numpy as np
import librosa
import soundfile as sf
import structlog
from typing import Dict, List, Optional
from collections import defaultdict

from app.services.detection_apis import AuddAPI, ACRCloudAPI, AcoustIDAPI

logger = structlog.get_logger()


class ComprehensiveBeatDetector:
    """
    Comprehensive beat detection using multiple sampling strategies
    and all available APIs for maximum accuracy
    """

    def __init__(self, apis: Dict):
        """
        Initialize detector with APIs

        Args:
            apis: Dictionary of API instances
        """
        self.apis = apis
        logger.info("comprehensive_detector_initialized", apis=list(apis.keys()))

    def detect(self, audio_file: str, min_confidence: float = 95.0) -> Dict:
        """
        Run comprehensive detection on audio file

        Args:
            audio_file: Path to audio file
            min_confidence: Minimum confidence threshold (default 95%)

        Returns:
            Detection results with confidence scores
        """
        start_time = time.time()

        # Validate audio_file parameter
        if audio_file is None:
            logger.error("audio_file_is_none")
            return {
                "status": "error",
                "error": "Audio file path is None",
                "processing_time": time.time() - start_time,
            }

        if not isinstance(audio_file, str):
            logger.error("audio_file_not_string", type=str(type(audio_file)))
            return {
                "status": "error",
                "error": f"Audio file path is not a string: {type(audio_file)}",
                "processing_time": time.time() - start_time,
            }

        if not os.path.exists(audio_file):
            logger.error("audio_file_not_found", path=audio_file)
            return {
                "status": "error",
                "error": f"Audio file not found: {audio_file}",
                "processing_time": time.time() - start_time,
            }

        logger.info(
            "comprehensive_detection_started",
            file=os.path.basename(audio_file),
            full_path=audio_file,
            min_confidence=min_confidence,
        )

        # Load audio - limit to first 60 seconds for performance
        try:
            # Only load first 60 seconds to keep detection time reasonable
            max_duration = 60.0
            print(f"[DEBUG] Loading audio from: {audio_file}")
            y, sr = librosa.load(audio_file, sr=44100, duration=max_duration)
            duration = librosa.get_duration(y=y, sr=sr)

            tempo, _ = librosa.beat.beat_track(y=y, sr=sr)
            detected_bpm = (
                float(tempo[0]) if hasattr(tempo, "__len__") else float(tempo)
            )

            print(f"[DEBUG] Audio loaded: duration={duration:.1f}s (limited to {max_duration}s), bpm={detected_bpm:.1f}, sample_rate={sr}")
            logger.info(
                "audio_loaded", duration=f"{duration:.1f}s", bpm=f"{detected_bpm:.1f}"
            )
        except Exception as e:
            import traceback
            traceback_str = traceback.format_exc()
            print(f"[ERROR] Audio load failed: {str(e)}")
            print(f"[ERROR] Traceback: {traceback_str}")
            logger.error("audio_load_failed", error=str(e), traceback=traceback_str)
            return {
                "status": "error",
                "error": f"Failed to load audio: {str(e)}",
                "processing_time": time.time() - start_time,
            }

        # Define sampling strategies based on audio duration
        # For short files (<10s), use the whole file as one segment
        # For longer files, use larger segments at intervals
        if duration < 10:
            # Short audio - just use the whole file
            strategies = [
                {"name": "full file", "duration": int(duration), "interval": int(duration)},
            ]
            print(f"[DEBUG] Short audio ({duration:.1f}s) - using full file strategy")
        else:
            strategies = [
                {"name": "10s segments every 10s", "duration": 10, "interval": 10},
                {"name": "15s segments every 15s", "duration": 15, "interval": 15},
            ]

        all_detections = []
        total_api_calls = 0

        # Test each strategy
        for strategy in strategies:
            seg_dur = strategy["duration"]
            interval = strategy["interval"]

            num_segments = len(range(0, int(duration) - seg_dur + 1, interval))
            print(f"[DEBUG] Starting strategy '{strategy['name']}': {num_segments} segments to test")

            # Create segments
            for start_sec in range(0, int(duration) - seg_dur + 1, interval):
                start_samples = int(start_sec * sr)
                end_samples = int((start_sec + seg_dur) * sr)

                if end_samples > len(y):
                    continue

                segment_audio = y[start_samples:end_samples]

                # Save segment to temp file
                with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
                    sf.write(tmp.name, segment_audio, sr)
                    temp_file = tmp.name

                # Test with all APIs
                for api_name, api in self.apis.items():
                    try:
                        result = api.recognize(temp_file)
                        total_api_calls += 1

                        if result:
                            detection = {
                                "segment": f"{start_sec}s",
                                "strategy": strategy["name"],
                                "segment_duration": seg_dur,
                                "api": api_name,
                                "artist": result["artist"],
                                "title": result["title"],
                                "confidence": result["confidence"],
                                "spotify_id": result.get("spotify_id"),
                                "album": result.get("album"),
                            }
                            all_detections.append(detection)

                        time.sleep(0.3)  # Rate limiting

                    except Exception as e:
                        logger.debug(f"detection_failed", api=api_name, error=str(e))
                        total_api_calls += 1

                # Cleanup temp file
                try:
                    os.unlink(temp_file)
                except:
                    pass

        # Calculate costs
        audd_calls = sum(1 for d in all_detections if d["api"] == "audd")
        acrcloud_calls = sum(1 for d in all_detections if d["api"] == "acrcloud")
        acoustid_calls = sum(1 for d in all_detections if d["api"] == "acoustid")

        total_cost = (audd_calls * 0.0035) + (acrcloud_calls * 0.006)

        print(f"[DEBUG] Detection loop completed. Total API calls: {total_api_calls}, Detections: {len(all_detections)}")

        logger.info(
            "detection_completed",
            total_calls=total_api_calls,
            detections=len(all_detections),
            cost=f"${total_cost:.4f}",
        )

        # No detections
        if not all_detections:
            return {
                "status": "no_match",
                "message": "No samples detected",
                "processing_time": time.time() - start_time,
                "api_stats": {
                    "total_calls": total_api_calls,
                    "total_cost": round(total_cost, 4),
                },
            }

        # Aggregate results by song
        songs = defaultdict(list)
        for det in all_detections:
            key = f"{det['artist']} - {det['title']}"
            songs[key].append(det)

        # Calculate confidence scores
        song_scores = []

        for song, detections in songs.items():
            # Count detections per API
            api_counts = defaultdict(int)
            for det in detections:
                api_counts[det["api"]] += 1

            # Count detections per strategy
            strategy_counts = defaultdict(int)
            for det in detections:
                strategy_counts[det["strategy"]] += 1

            # Unique segments
            unique_segments = len(set(d["segment"] for d in detections))

            # Calculate weighted confidence
            base_confidence = np.mean([d["confidence"] for d in detections])

            # Bonuses - only give bonuses for multiple detections/APIs/strategies
            # Single detection should NOT get bonuses
            api_bonus = 0
            if len(api_counts) >= 2:
                api_bonus = min(len(api_counts) * 10, 30)

            strategy_bonus = 0
            if len(strategy_counts) >= 2:
                strategy_bonus = min(len(strategy_counts) * 5, 20)

            segment_bonus = 0
            if len(detections) >= 3:  # Need at least 3 detections for bonus
                segment_bonus = min((len(detections) - 2) * 3, 25)

            # Final confidence (capped at 100)
            final_confidence = min(
                base_confidence + api_bonus + strategy_bonus + segment_bonus, 100
            )

            song_scores.append(
                {
                    "song": song,
                    "artist": detections[0]["artist"],
                    "title": detections[0]["title"],
                    "detections": detections,
                    "count": len(detections),
                    "unique_segments": unique_segments,
                    "apis": list(api_counts.keys()),
                    "api_counts": dict(api_counts),
                    "strategies": list(strategy_counts.keys()),
                    "base_confidence": base_confidence,
                    "final_confidence": final_confidence,
                    "spotify_id": next(
                        (d["spotify_id"] for d in detections if d.get("spotify_id")),
                        None,
                    ),
                    "album": next(
                        (d["album"] for d in detections if d.get("album")), None
                    ),
                    "isrc": next(
                        (d["isrc"] for d in detections if d.get("isrc")), None
                    ),
                }
            )

        # Sort by confidence
        song_scores.sort(key=lambda x: x["final_confidence"], reverse=True)

        # Filter by minimum confidence
        high_confidence_matches = [
            score
            for score in song_scores
            if score["final_confidence"] >= min_confidence
        ]

        processing_time = time.time() - start_time

        # Return results
        if not high_confidence_matches:
            return {
                "status": "low_confidence",
                "message": f"No matches found with confidence >= {min_confidence}%",
                "highest_confidence": (
                    song_scores[0]["final_confidence"] if song_scores else 0
                ),
                "processing_time": processing_time,
                "api_stats": {
                    "total_calls": total_api_calls,
                    "audd_calls": audd_calls,
                    "acrcloud_calls": acrcloud_calls,
                    "acoustid_calls": acoustid_calls,
                    "total_cost": round(total_cost, 4),
                },
                "all_matches": [
                    {"song": score["song"], "confidence": score["final_confidence"]}
                    for score in song_scores[:5]  # Top 5
                ],
            }

        # High confidence match(es) found
        top_match = high_confidence_matches[0]

        return {
            "status": "match_found",
            "match": {
                "artist": top_match["artist"],
                "title": top_match["title"],
                "album": top_match["album"],
                "confidence": round(top_match["final_confidence"], 1),
                "base_confidence": round(top_match["base_confidence"], 1),
                "detections": top_match["count"],
                "unique_segments": top_match["unique_segments"],
                "apis_used": top_match["apis"],
                "api_breakdown": top_match["api_counts"],
                "strategies_confirmed": len(top_match["strategies"]),
                "spotify_id": top_match["spotify_id"],
                "spotify_url": (
                    f"https://open.spotify.com/track/{top_match['spotify_id']}"
                    if top_match["spotify_id"]
                    else None
                ),
            },
            "alternative_matches": (
                [
                    {
                        "artist": score["artist"],
                        "title": score["title"],
                        "album": score.get("album"),
                        "confidence": round(score["final_confidence"], 1),
                        "detections": score["count"],
                        "spotify_id": score.get("spotify_id"),
                        "isrc": score.get("isrc"),
                    }
                    for score in high_confidence_matches[1:20]  # Next 19
                ]
                if len(high_confidence_matches) > 1
                else []
            ),
            "processing_time": processing_time,
            "api_stats": {
                "total_calls": total_api_calls,
                "audd_calls": audd_calls,
                "acrcloud_calls": acrcloud_calls,
                "acoustid_calls": acoustid_calls,
                "total_cost": round(total_cost, 4),
            },
        }
