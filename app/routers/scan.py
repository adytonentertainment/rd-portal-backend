import concurrent.futures
import os
import shutil
import urllib.parse
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import List, Optional

import requests
import stripe
from app.database import get_session
from app.libs.ACRCloud.v2 import get_acrcloud_api
from app.logger import get_logger

logger = get_logger("scan")
from app.misc import get_spotify_access_token
from app.misc.misc import get_scan_threshold, thresholds
from app.models.models import (
    ACRCloudScan,
    BatchUpload,
    BatchUploadItem,
    BatchUploadItemStatus,
    BatchUploadStatus,
    Client,
    User,
)
from app.routers.auth import get_user
from app.schemas.acrcloud import ShowContainerResponse, Song
from app.services.comprehensive_detector import ComprehensiveBeatDetector
from app.services.detection_apis import AcoustIDAPI, ACRCloudAPI, AuddAPI
from app.services.notification_service import NotificationService
from app.settings import get_settings
from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    HTTPException,
    Query,
    UploadFile,
    status,
)
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified

# Directory to store audio files for rescans
SCANS_UPLOAD_DIR = Path(__file__).parent.parent.parent / "uploads" / "scans"
SCANS_UPLOAD_DIR.mkdir(parents=True, exist_ok=True)


def _get_spotify_album_art(spotify_track_id: str) -> str:
    """Fetch album art URL from Spotify using track ID."""
    if not spotify_track_id:
        return ""
    try:
        token = get_spotify_access_token()
        if not token:
            return ""
        headers = {"Authorization": f"Bearer {token}"}
        response = requests.get(
            f"https://api.spotify.com/v1/tracks/{spotify_track_id}",
            headers=headers,
            timeout=5,
        )
        if response.ok:
            data = response.json()
            images = data.get("album", {}).get("images", [])
            if images:
                return images[0].get("url", "")
    except Exception as e:
        logger.warning(f"Failed to fetch album art for {spotify_track_id}: {e}")
    return ""


def verify_subscription(current_user: User = Depends(get_user)):
    # Get the active subscription for the current stripe mode
    subscription = current_user.get_active_subscription()
    if not subscription:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="You are not subscribed."
        )
    return current_user


acrcloud = get_acrcloud_api()
scan_router = APIRouter(
    prefix="/scan",
    tags=["Scan"],
)


# Initialize comprehensive detector
def get_comprehensive_detector():
    """Initialize comprehensive beat detector with API keys from settings"""
    from app.settings import get_settings

    settings = get_settings()

    apis = {}

    if settings.audd_api_key:
        apis["audd"] = AuddAPI(settings.audd_api_key)

    if settings.acrcloud_access_key and settings.acrcloud_access_secret:
        apis["acrcloud"] = ACRCloudAPI(
            settings.acrcloud_access_key,
            settings.acrcloud_access_secret,
            "identify-us-west-2.acrcloud.com",
        )

    if settings.acoustid_key:
        apis["acoustid"] = AcoustIDAPI(settings.acoustid_key)

    return ComprehensiveBeatDetector(apis)


def run_comprehensive_detection_background(
    file_path: str,
    file_id: str,
    user_id: int,
    is_auto: bool = False,
    keep_file: bool = False,
    previous_matches: list = None,
):
    """
    Run comprehensive detection in background and update database.

    Args:
        file_path: Path to audio file
        file_id: ACRCloud file ID
        user_id: User ID
        is_auto: Whether this is an auto-rescan (affects notification type)
        keep_file: Whether to keep the file after processing (for stored files)
        previous_matches: List of previous match keys (title|artist) for auto-rescan comparison
    """
    if previous_matches is None:
        previous_matches = []
    print(f"[DEBUG] Background task started for {file_id} (is_auto={is_auto})")
    try:
        from app.database import SessionLocal

        detector = get_comprehensive_detector()
        print(f"[DEBUG] Detector initialized for {file_id}")

        settings = get_settings()
        min_confidence = settings.tunescan_min_confidence
        detection_result = detector.detect(file_path, min_confidence=min_confidence)
        print(
            f"[DEBUG] Detection completed for {file_id}: {detection_result.get('status')}"
        )

        db = SessionLocal()
        print(f"[DEBUG] Database session created for {file_id}")

        try:
            scan = (
                db.query(ACRCloudScan)
                .filter(
                    ACRCloudScan.acrcloud_file_id == file_id,
                    ACRCloudScan.user_id == user_id,
                )
                .first()
            )

            print(f"[DEBUG] Scan query result for {file_id}: {scan is not None}")

            if scan:
                print(f"[DEBUG] Updating scan {scan.id} with detection result")
                scan.comprehensive_detection = detection_result
                # Reset the auto-rescan flag after processing
                scan.is_pending_auto_rescan = False
                flag_modified(scan, "comprehensive_detection")
                db.commit()
                db.refresh(scan)
                print(
                    f"[INFO] Comprehensive detection saved for {file_id}: {detection_result.get('status')}"
                )

                # Create notification for scan completion
                try:
                    notification_service = NotificationService(db)
                    match_found = detection_result.get("status") == "match_found"
                    match_data = (
                        detection_result.get("match", {}) if match_found else {}
                    )

                    if is_auto and match_found:
                        # Auto-rescan with match found - only notify for NEW matches
                        # Collect all matches (primary + alternatives)
                        all_matches = []
                        if match_data:
                            all_matches.append(match_data)
                        if detection_result.get("alternative_matches"):
                            all_matches.extend(detection_result["alternative_matches"])

                        # Convert previous_matches to a set for fast lookup
                        previous_match_set = set(previous_matches)
                        print(f"[DEBUG] Previous matches: {previous_match_set}")

                        # Create a notification only for NEW unique matches
                        seen_songs = set()  # Deduplicate by title+artist
                        new_match_count = 0
                        for match in all_matches:
                            match_key = f"{match.get('title', 'Unknown')}|{match.get('artist', 'Unknown')}"
                            # Only notify if: not a duplicate in this scan AND not in previous matches
                            if (
                                match_key not in seen_songs
                                and match_key not in previous_match_set
                            ):
                                seen_songs.add(match_key)
                                notification_service.create_auto_scan_match_notification(
                                    user_id=user_id,
                                    match_title=match.get("title", "Unknown"),
                                    match_artist=match.get("artist", "Unknown"),
                                    platform="Spotify",
                                    confidence=match.get("confidence", 0),
                                )
                                new_match_count += 1
                                print(
                                    f"[INFO] Created notification for NEW match: {match.get('title')} by {match.get('artist')}"
                                )
                            elif match_key in previous_match_set:
                                print(
                                    f"[DEBUG] Skipping notification - match already existed: {match_key}"
                                )

                        if new_match_count == 0:
                            print(
                                f"[INFO] Auto-rescan completed - no NEW matches found (all {len(all_matches)} matches already existed)"
                            )
                    elif not is_auto and match_found:
                        # Regular upload scan with match found - create ONE notification PER match
                        # Collect all matches (primary + alternatives)
                        all_matches = []
                        if match_data:
                            all_matches.append(match_data)
                        if detection_result.get("alternative_matches"):
                            all_matches.extend(detection_result["alternative_matches"])

                        # Create a notification for each unique match
                        seen_songs = set()  # Deduplicate by title+artist
                        for match in all_matches:
                            match_key = f"{match.get('title', 'Unknown')}|{match.get('artist', 'Unknown')}"
                            if match_key not in seen_songs:
                                seen_songs.add(match_key)
                                notification_service.create_upload_match_notification(
                                    user_id=user_id,
                                    match_title=match.get("title", "Unknown"),
                                    match_artist=match.get("artist", "Unknown"),
                                    platform="Spotify",
                                    confidence=match.get("confidence", 0),
                                )
                                print(
                                    f"[INFO] Created upload notification for match: {match.get('title')} by {match.get('artist')}"
                                )
                    # If is_auto but no match found, no notification is created
                except Exception as notif_error:
                    print(f"[WARN] Failed to create scan notification: {notif_error}")
            else:
                print(f"[ERROR] No scan found for file_id={file_id}, user_id={user_id}")
        except Exception as db_error:
            print(
                f"[ERROR] Database error for {file_id}: {type(db_error).__name__}: {str(db_error)}"
            )
            db.rollback()
            import traceback

            traceback.print_exc()
        finally:
            db.close()
    except Exception as e:
        print(
            f"[ERROR] Background detection failed for {file_id}: {type(e).__name__}: {str(e)}"
        )
        import traceback

        traceback.print_exc()

        # Save error status so scan doesn't stay stuck as "Processing..."
        try:
            from app.database import SessionLocal

            db = SessionLocal()
            scan = (
                db.query(ACRCloudScan)
                .filter(
                    ACRCloudScan.acrcloud_file_id == file_id,
                    ACRCloudScan.user_id == user_id,
                )
                .first()
            )
            if scan and scan.comprehensive_detection is None:
                scan.comprehensive_detection = {
                    "status": "error",
                    "message": f"Detection failed: {str(e)}",
                    "error_type": type(e).__name__,
                }
                flag_modified(scan, "comprehensive_detection")
                db.commit()
                print(f"[INFO] Saved error status for {file_id}")
            db.close()
        except Exception as save_error:
            print(f"[ERROR] Failed to save error status: {save_error}")
    finally:
        # Clean up temp file (but not stored files)
        if not keep_file:
            try:
                os.remove(file_path)
            except:
                pass


@scan_router.get("/tracks")
async def get_users_tracks(
    page: int = 1,
    per_page: int = 10,
    client_id: Optional[int] = Query(None, description="Filter by client ID"),
    user: User = Depends(verify_subscription),
    db: Session = Depends(get_session),
):
    try:
        # Filter scans by client_id if provided
        query = db.query(ACRCloudScan).filter(ACRCloudScan.user_id == user.id)
        if client_id is not None:
            query = query.filter(ACRCloudScan.client_id == client_id)
        scans = query.all()
        file_ids = [scan.acrcloud_file_id for scan in scans]
        print(f"[DEBUG] Fetching tracks for user {user.id}, file_ids: {file_ids}")
        acrcloud_response = acrcloud.showContainerContents(file_ids, page, per_page)
        # Handle both dict and object responses from ACRCloud
        is_dict = isinstance(acrcloud_response, dict)
        songs_list = (
            acrcloud_response.get("songs", [])
            if is_dict
            else (
                acrcloud_response.songs if hasattr(acrcloud_response, "songs") else []
            )
        )

        print(f"[DEBUG] ACRCloud returned {len(songs_list)} songs")

        # Replace ACRCloud matches with comprehensive detection results only
        # If comprehensive detection is still pending (NULL), show "Processing..." status
        if acrcloud_response and songs_list:
            songs_to_keep = []

            for song in songs_list:
                # Handle both dict and object format
                if isinstance(song, dict):
                    file_id = song.get("file_id")
                    filename = song.get("filename")
                    loading = song.get("loading", False)
                    tracks = song.get("tracks", [])
                else:
                    file_id = song.file_id if hasattr(song, "file_id") else None
                    filename = song.filename if hasattr(song, "filename") else None
                    loading = song.loading if hasattr(song, "loading") else False
                    tracks = song.tracks if hasattr(song, "tracks") else []

                print(f"[DEBUG] Processing song: {file_id}")
                try:
                    # Create song dict with existing data
                    song_dict = (
                        song
                        if isinstance(song, dict)
                        else {
                            "filename": filename,
                            "file_id": file_id,
                            "loading": loading,
                            "tracks": tracks,
                        }
                    )

                    # IMPORTANT: Clear ACRCloud's tracks first to prevent showing simple search results
                    song_dict["tracks"] = []

                    # Find comprehensive detection for this file_id
                    scan = (
                        db.query(ACRCloudScan)
                        .filter(
                            ACRCloudScan.acrcloud_file_id == file_id,
                            ACRCloudScan.user_id == user.id,
                        )
                        .first()
                    )

                    # If no scan record exists, this is an old file from before comprehensive detection - skip it
                    if not scan:
                        print(f"[DEBUG] {file_id} - no scan record, skipping old file")
                        continue

                    # If scan exists but comprehensive_detection is NULL, show "Processing..." status
                    if scan.comprehensive_detection is None:
                        print(
                            f"[DEBUG] {file_id} - comprehensive detection still processing"
                        )
                        song_dict["loading"] = True
                        song_dict["tracks"] = []  # Empty tracks while processing
                        songs_to_keep.append(song_dict)
                        continue

                    # If detection failed, show error status
                    if scan.comprehensive_detection.get("status") == "error":
                        print(
                            f"[DEBUG] {file_id} - detection failed: {scan.comprehensive_detection.get('message')}"
                        )
                        song_dict["loading"] = False
                        song_dict["error"] = True
                        song_dict["error_message"] = scan.comprehensive_detection.get(
                            "message", "Detection failed"
                        )
                        song_dict["tracks"] = []
                        songs_to_keep.append(song_dict)
                        continue

                    # If we have comprehensive detection results, show up to 4 results with confidence
                    if (
                        scan
                        and scan.comprehensive_detection
                        and isinstance(scan.comprehensive_detection, dict)
                    ):
                        if scan.comprehensive_detection.get("status") == "match_found":
                            comp_match = scan.comprehensive_detection["match"]

                            # Replace tracks with comprehensive detection results (primary match)
                            spotify_id = comp_match.get("spotify_id", "")
                            album_art = (
                                _get_spotify_album_art(spotify_id) if spotify_id else ""
                            )
                            song_dict["tracks"] = [
                                {
                                    "artist_name": comp_match["artist"],
                                    "song_name": comp_match["title"],
                                    "album": comp_match.get("album", ""),
                                    "isrc": comp_match.get("isrc", ""),
                                    "spotify_id": spotify_id,
                                    "album_art": album_art,
                                    "confidence": comp_match["confidence"],
                                    "comprehensive": True,
                                    "type": "music",
                                    "duration": 0.0,
                                    "spotify_link": f"https://open.spotify.com/track/{spotify_id}"
                                    if spotify_id
                                    else "",
                                    "applemusic_link": "",
                                    "deezer_link": "",
                                    "youtube_link": "",
                                }
                            ]

                            # Add alternative matches (up to 19 more, total 20 results max)
                            if scan.comprehensive_detection.get("alternative_matches"):
                                for alt in scan.comprehensive_detection[
                                    "alternative_matches"
                                ][:19]:
                                    alt_spotify_id = alt.get("spotify_id", "")
                                    alt_album_art = (
                                        _get_spotify_album_art(alt_spotify_id)
                                        if alt_spotify_id
                                        else ""
                                    )
                                    song_dict["tracks"].append(
                                        {
                                            "artist_name": alt["artist"],
                                            "song_name": alt["title"],
                                            "album": alt.get("album", ""),
                                            "isrc": alt.get("isrc", ""),
                                            "spotify_id": alt_spotify_id,
                                            "album_art": alt_album_art,
                                            "confidence": alt["confidence"],
                                            "comprehensive": True,
                                            "type": "music",
                                            "duration": 0.0,
                                            "spotify_link": f"https://open.spotify.com/track/{alt_spotify_id}"
                                            if alt_spotify_id
                                            else "",
                                            "applemusic_link": "",
                                            "deezer_link": "",
                                            "youtube_link": "",
                                        }
                                    )
                        elif scan.comprehensive_detection.get("status") == "no_match":
                            # Comprehensive detection completed but found no matches
                            # Return empty tracks array - no fake results
                            song_dict["tracks"] = []

                    # Keep this song in the results
                    song_dict["loading"] = False  # Not loading anymore
                    songs_to_keep.append(song_dict)

                except Exception as song_error:
                    logger.error(
                        f"Error processing comprehensive detection for song: {song_error}"
                    )
                    # On error, show error message
                    error_song = {
                        "filename": (
                            song.filename if hasattr(song, "filename") else "Unknown"
                        ),
                        "file_id": (
                            song.file_id if hasattr(song, "file_id") else "unknown"
                        ),
                        "loading": False,
                        "tracks": [
                            {
                                "artist_name": "Error",
                                "song_name": "Error processing detection",
                                "confidence": 0,
                                "type": "music",
                                "duration": 0.0,
                                "isrc": "",
                                "spotify_link": "",
                                "applemusic_link": "",
                                "deezer_link": "",
                                "youtube_link": "",
                            }
                        ],
                    }
                    songs_to_keep.append(error_song)
                    continue

            # Update the response with only the songs that should be displayed
            if isinstance(acrcloud_response, dict):
                acrcloud_response["songs"] = songs_to_keep
            else:
                acrcloud_response.songs = songs_to_keep

        # Convert to dict to allow flexible field structure (processing status, etc.)
        if hasattr(acrcloud_response, "dict"):
            return acrcloud_response.dict()
        elif isinstance(acrcloud_response, dict):
            return acrcloud_response
        else:
            return {
                "songs": [],
                "current_page": page,
                "last_page": 0,
                "per_page": per_page,
            }
    except Exception as e:
        logger.error(f"Error in get_users_tracks: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@scan_router.get("/tracks/{file_id}", response_model=ShowContainerResponse)
async def get_single_track(
    file_id: str,
    user: User = Depends(verify_subscription),
    db: Session = Depends(get_session),
):
    # Get ACRCloud data
    acrcloud_response = acrcloud.showContainerContents([file_id])

    # Get comprehensive detection from database
    scan = (
        db.query(ACRCloudScan)
        .filter(
            ACRCloudScan.acrcloud_file_id == file_id, ACRCloudScan.user_id == user.id
        )
        .first()
    )

    # If we have comprehensive detection results, replace ACRCloud matches with them
    if (
        scan
        and scan.comprehensive_detection
        and scan.comprehensive_detection.get("status") == "match_found"
    ):
        comp_match = scan.comprehensive_detection["match"]

        # Transform comprehensive detection to ACRCloud format for frontend compatibility
        if (
            acrcloud_response
            and hasattr(acrcloud_response, "songs")
            and acrcloud_response.songs
        ):
            # Replace the matches in the first song
            spotify_id = comp_match.get("spotify_id", "")
            album_art = _get_spotify_album_art(spotify_id) if spotify_id else ""
            acrcloud_response.songs[0].tracks = [
                {
                    "artist_name": comp_match["artist"],
                    "song_name": comp_match["title"],
                    "album": comp_match.get("album"),
                    "isrc": None,
                    "spotify_id": spotify_id,
                    "album_art": album_art,
                    "confidence": comp_match["confidence"],
                    "comprehensive": True,  # Flag to indicate this is from comprehensive detection
                }
            ]

            # Add alternative matches if any
            if scan.comprehensive_detection.get("alternative_matches"):
                for alt in scan.comprehensive_detection["alternative_matches"]:
                    alt_spotify_id = alt.get("spotify_id", "")
                    alt_album_art = (
                        _get_spotify_album_art(alt_spotify_id) if alt_spotify_id else ""
                    )
                    acrcloud_response.songs[0].tracks.append(
                        {
                            "artist_name": alt["artist"],
                            "song_name": alt["title"],
                            "album": alt.get("album"),
                            "isrc": None,
                            "spotify_id": alt_spotify_id,
                            "album_art": alt_album_art,
                            "confidence": alt["confidence"],
                            "comprehensive": True,
                        }
                    )

    return acrcloud_response


@scan_router.post("/tracks", response_model=Song)
async def upload_track(
    file: UploadFile,
    client_id: Optional[int] = Query(
        None, description="Client ID to associate the scan with"
    ),
    user: User = Depends(verify_subscription),
    db: Session = Depends(get_session),
):
    # Get the active subscription for the current stripe mode
    subscription = user.get_active_subscription()
    temp = NamedTemporaryFile(delete=False)
    try:
        try:
            contents = file.file.read()

            # File type check for .mp3 by inspecting the header
            # MP3 files typically begin with ID3 or 0xFFFB / 0xFFF3 / 0xFFF2 sync
            is_mp3 = False
            if contents[:3] == b"ID3":
                is_mp3 = True
            elif len(contents) > 2 and (
                contents[0] == 0xFF and (contents[1] & 0xE0) == 0xE0
            ):
                is_mp3 = True

            if not is_mp3:
                raise HTTPException(
                    status_code=400, detail="Only valid MP3 files are supported."
                )

            with temp as f:
                f.write(contents)
        except HTTPException:
            raise
        except:
            raise HTTPException(
                status_code=500, detail="An error occured while uploading the file."
            )
        finally:
            file.file.close()

        # user will exceed his limit. make him confirm this
        if (
            not subscription.limit_exceeded
            and subscription.scans + 1
            > get_scan_threshold(
                str(subscription.tier), subscription.billing_interval or "month"
            )
        ):
            raise HTTPException(
                status_code=403,
                detail="You need to confirm that you want to exceed the limit of your subscription.",
            )

        response = acrcloud.uploadFile(temp.name, file.filename)

        scan = ACRCloudScan(
            user_id=user.id, acrcloud_file_id=response.file_id, client_id=client_id
        )

        db.add(scan)

        # send to stripe and database
        stripe.billing.MeterEvent.create(
            event_name="scans",
            payload={"value": 1, "stripe_customer_id": user.stripe_customer_id},
        )
        subscription.scans += 1
        db.commit()

    except Exception as e:
        logger.error(e)
        raise HTTPException(status_code=e.status_code, detail=str(e))
    finally:
        os.remove(temp.name)
    return response


@scan_router.post("/tracks/comprehensive")
async def upload_track_comprehensive(
    background_tasks: BackgroundTasks,
    file: UploadFile,
    client_id: Optional[int] = Query(
        None, description="Client ID to associate the scan with"
    ),
    user: User = Depends(verify_subscription),
    db: Session = Depends(get_session),
):
    """
    Upload and analyze track using comprehensive beat detection
    Returns immediately after upload, runs detection in background
    """
    # Get the active subscription for the current stripe mode
    subscription = user.get_active_subscription()
    temp = NamedTemporaryFile(delete=False)
    temp_path = temp.name

    try:
        try:
            contents = file.file.read()
            with temp as f:
                f.write(contents)
        except:
            raise HTTPException(
                status_code=500, detail="An error occurred while uploading the file."
            )
        finally:
            file.file.close()

        # Check subscription limits
        if (
            not subscription.limit_exceeded
            and subscription.scans + 1
            > get_scan_threshold(
                str(subscription.tier), subscription.billing_interval or "month"
            )
        ):
            raise HTTPException(
                status_code=403,
                detail="You need to confirm that you want to exceed the limit of your subscription.",
            )

        # Upload to ACRCloud for catalog/storage
        acrcloud_response = acrcloud.uploadFile(temp_path, file.filename)

        # Store file permanently for future rescans
        stored_file_path = SCANS_UPLOAD_DIR / f"{acrcloud_response.file_id}.mp3"
        shutil.copy2(temp_path, stored_file_path)

        # Save to database (comprehensive_detection will be updated by background task)
        scan = ACRCloudScan(
            user_id=user.id,
            acrcloud_file_id=acrcloud_response.file_id,
            client_id=client_id,
            comprehensive_detection=None,  # Will be filled by background task
            stored_file_path=str(stored_file_path),  # Store path for rescans
        )
        db.add(scan)

        # Update stripe and database
        stripe.billing.MeterEvent.create(
            event_name="scans",
            payload={"value": 1, "stripe_customer_id": user.stripe_customer_id},
        )
        subscription.scans += 1
        db.flush()  # Ensure scan record is written to DB
        db.commit()
        db.refresh(scan)  # Refresh to ensure scan is fully persisted

        # Copy temp file for background processing (original will be deleted)
        background_temp = temp_path + "_bg"
        shutil.copy2(temp_path, background_temp)

        # Schedule comprehensive detection in background
        background_tasks.add_task(
            run_comprehensive_detection_background,
            background_temp,
            acrcloud_response.file_id,
            user.id,
            False,  # is_auto=False for initial upload
            False,  # keep_file=False for temp background file
        )

        print(
            f"[INFO] Comprehensive detection scheduled for {acrcloud_response.file_id}"
        )

        # Return immediately
        return acrcloud_response

    except Exception as e:
        logger.error(f"Upload error: {e}")
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        # Clean up original temp file
        try:
            os.remove(temp_path)
        except:
            pass


# Maximum files allowed in a single bulk upload
MAX_BULK_FILES = 50


def process_single_batch_item(
    item_id: int,
    file_path: str,
    filename: str,
    user_id: int,
    client_id: Optional[int],
    batch_id: int,
):
    """Process a single file within a batch upload"""
    from app.database import SessionLocal

    db = SessionLocal()
    try:
        item = db.query(BatchUploadItem).filter(BatchUploadItem.id == item_id).first()
        if not item or item.status == BatchUploadItemStatus.CANCELLED:
            return

        # Update status to uploading
        item.status = BatchUploadItemStatus.UPLOADING
        db.commit()

        # Upload to ACRCloud
        acrcloud_response = acrcloud.uploadFile(file_path, filename)

        # Store file permanently (preserve original extension)
        file_ext = os.path.splitext(filename)[1].lower() or ".mp3"
        stored_file_path = SCANS_UPLOAD_DIR / f"{acrcloud_response.file_id}{file_ext}"
        shutil.copy2(file_path, stored_file_path)

        # Create scan record
        scan = ACRCloudScan(
            user_id=user_id,
            acrcloud_file_id=acrcloud_response.file_id,
            client_id=client_id,
            comprehensive_detection=None,
            stored_file_path=str(stored_file_path),
        )
        db.add(scan)
        db.flush()

        # Update batch item
        item.status = BatchUploadItemStatus.PROCESSING
        item.scan_id = scan.id
        item.acrcloud_file_id = acrcloud_response.file_id
        db.commit()

        # Run comprehensive detection
        detector = get_comprehensive_detector()
        settings = get_settings()
        min_confidence = settings.tunescan_min_confidence
        detection_result = detector.detect(file_path, min_confidence=min_confidence)

        # Update scan with detection results
        scan.comprehensive_detection = detection_result
        flag_modified(scan, "comprehensive_detection")

        # Update item as completed
        item.status = BatchUploadItemStatus.COMPLETED
        db.commit()

        # Create notification if match found
        if detection_result.get("status") == "match_found":
            notification_service = NotificationService(db)
            match_data = detection_result.get("match", {})
            notification_service.create_upload_match_notification(
                user_id=user_id,
                match_title=match_data.get("title", "Unknown"),
                match_artist=match_data.get("artist", "Unknown"),
                platform="Spotify",
                confidence=match_data.get("confidence", 0),
            )

    except Exception as e:
        # Mark as failed
        item = db.query(BatchUploadItem).filter(BatchUploadItem.id == item_id).first()
        if item:
            item.status = BatchUploadItemStatus.FAILED
            item.error_message = str(e)[:500]
            db.commit()
        print(f"[ERROR] Batch item {item_id} failed: {e}")
    finally:
        # Cleanup temp file
        try:
            os.remove(file_path)
        except:
            pass
        db.close()


def process_batch_upload_background(
    batch_id: int,
    file_data: List[tuple],  # List of (item_id, temp_path, filename)
    user_id: int,
    client_id: Optional[int],
):
    """Process entire batch in background with parallel execution"""
    from app.database import SessionLocal

    db = SessionLocal()
    try:
        batch = db.query(BatchUpload).filter(BatchUpload.id == batch_id).first()
        batch.status = BatchUploadStatus.PROCESSING
        db.commit()
        db.close()

        # Process files with limited concurrency (3 at a time for API rate limits)
        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as executor:
            futures = []
            for item_id, file_path, filename in file_data:
                future = executor.submit(
                    process_single_batch_item,
                    item_id,
                    file_path,
                    filename,
                    user_id,
                    client_id,
                    batch_id,
                )
                futures.append(future)

            # Wait for all to complete
            concurrent.futures.wait(futures)

        # Update batch final status
        db = SessionLocal()
        batch = db.query(BatchUpload).filter(BatchUpload.id == batch_id).first()

        completed = (
            db.query(BatchUploadItem)
            .filter(
                BatchUploadItem.batch_id == batch_id,
                BatchUploadItem.status == BatchUploadItemStatus.COMPLETED,
            )
            .count()
        )

        failed = (
            db.query(BatchUploadItem)
            .filter(
                BatchUploadItem.batch_id == batch_id,
                BatchUploadItem.status == BatchUploadItemStatus.FAILED,
            )
            .count()
        )

        cancelled = (
            db.query(BatchUploadItem)
            .filter(
                BatchUploadItem.batch_id == batch_id,
                BatchUploadItem.status == BatchUploadItemStatus.CANCELLED,
            )
            .count()
        )

        batch.completed_files = completed
        batch.failed_files = failed
        batch.cancelled_files = cancelled

        if completed == batch.total_files:
            batch.status = BatchUploadStatus.COMPLETED
        elif completed > 0:
            batch.status = BatchUploadStatus.PARTIALLY_COMPLETED
        elif failed == batch.total_files:
            batch.status = BatchUploadStatus.FAILED
        else:
            batch.status = BatchUploadStatus.COMPLETED

        db.commit()
        print(
            f"[INFO] Batch {batch_id} completed: {completed}/{batch.total_files} successful"
        )

    except Exception as e:
        print(f"[ERROR] Batch processing failed: {e}")
        db = SessionLocal()
        batch = db.query(BatchUpload).filter(BatchUpload.id == batch_id).first()
        if batch:
            batch.status = BatchUploadStatus.FAILED
            db.commit()
    finally:
        db.close()


@scan_router.post("/tracks/bulk")
async def upload_tracks_bulk(
    background_tasks: BackgroundTasks,
    files: List[UploadFile],
    client_id: Optional[int] = Query(
        None, description="Client ID to associate scans with"
    ),
    user: User = Depends(verify_subscription),
    db: Session = Depends(get_session),
):
    """
    Upload multiple tracks for comprehensive detection (max 50 files).
    Returns batch_id immediately, process files in background.
    Poll /scan/tracks/bulk/{batch_id} for status updates.
    """
    # Validate file count
    if len(files) > MAX_BULK_FILES:
        raise HTTPException(
            status_code=400,
            detail=f"Maximum {MAX_BULK_FILES} files allowed per batch upload.",
        )

    if len(files) == 0:
        raise HTTPException(status_code=400, detail="No files provided.")

    subscription = user.get_active_subscription()

    # Check subscription limits for entire batch
    scan_limit = get_scan_threshold(
        str(subscription.tier), subscription.billing_interval or "month"
    )
    if not subscription.limit_exceeded and subscription.scans + len(files) > scan_limit:
        raise HTTPException(
            status_code=403,
            detail=f"Uploading {len(files)} files would exceed your plan limit. "
            f"You have {scan_limit - subscription.scans} scans remaining. "
            "Please confirm to exceed the limit or reduce the number of files.",
        )

    # Validate all files are MP3 or WAV
    valid_files = []
    valid_extensions = (".mp3", ".wav")
    for f in files:
        if not f.filename.lower().endswith(valid_extensions):
            raise HTTPException(
                status_code=400,
                detail=f"File '{f.filename}' is not a supported format. Only MP3 and WAV files are supported.",
            )
        valid_files.append(f)

    # Create batch record
    batch = BatchUpload(
        user_id=user.id,
        client_id=client_id,
        status=BatchUploadStatus.PENDING,
        total_files=len(valid_files),
    )
    db.add(batch)
    db.flush()

    # Save files and create batch items
    file_data = []
    for f in valid_files:
        # Save to temp file
        # Get file extension from original filename
        file_ext = os.path.splitext(f.filename)[1].lower() or ".mp3"
        temp = NamedTemporaryFile(delete=False, suffix=file_ext)
        try:
            contents = f.file.read()
            temp.write(contents)
            temp.close()
        finally:
            f.file.close()

        # Create batch item
        item = BatchUploadItem(
            batch_id=batch.id,
            filename=f.filename,
            status=BatchUploadItemStatus.PENDING,
        )
        db.add(item)
        db.flush()

        file_data.append((item.id, temp.name, f.filename))

    # Update Stripe metering for all files
    stripe.billing.MeterEvent.create(
        event_name="scans",
        payload={
            "value": len(valid_files),
            "stripe_customer_id": user.stripe_customer_id,
        },
    )
    subscription.scans += len(valid_files)

    db.commit()

    # Schedule background processing
    background_tasks.add_task(
        process_batch_upload_background,
        batch.id,
        file_data,
        user.id,
        client_id,
    )

    return {
        "batch_id": batch.id,
        "message": f"Batch upload started with {len(valid_files)} files",
        "total_files": len(valid_files),
    }


@scan_router.get("/tracks/bulk/{batch_id}")
async def get_batch_status(
    batch_id: int,
    user: User = Depends(verify_subscription),
    db: Session = Depends(get_session),
):
    """Get status of a batch upload with individual file statuses"""
    batch = (
        db.query(BatchUpload)
        .filter(BatchUpload.id == batch_id, BatchUpload.user_id == user.id)
        .first()
    )

    if not batch:
        raise HTTPException(status_code=404, detail="Batch not found")

    items = (
        db.query(BatchUploadItem)
        .filter(BatchUploadItem.batch_id == batch_id)
        .order_by(BatchUploadItem.id)
        .all()
    )

    return {
        "batch_id": batch.id,
        "status": batch.status.value
        if hasattr(batch.status, "value")
        else str(batch.status),
        "total_files": batch.total_files,
        "completed_files": batch.completed_files,
        "failed_files": batch.failed_files,
        "cancelled_files": batch.cancelled_files,
        "created_at": batch.created_at.isoformat(),
        "items": [
            {
                "id": item.id,
                "filename": item.filename,
                "status": item.status.value
                if hasattr(item.status, "value")
                else str(item.status),
                "error_message": item.error_message,
                "acrcloud_file_id": item.acrcloud_file_id,
            }
            for item in items
        ],
    }


@scan_router.delete("/tracks/bulk/{batch_id}")
async def cancel_batch_upload(
    batch_id: int,
    user: User = Depends(verify_subscription),
    db: Session = Depends(get_session),
):
    """Cancel pending items in a batch upload"""
    batch = (
        db.query(BatchUpload)
        .filter(BatchUpload.id == batch_id, BatchUpload.user_id == user.id)
        .first()
    )

    if not batch:
        raise HTTPException(status_code=404, detail="Batch not found")

    # Cancel only pending items
    pending_items = (
        db.query(BatchUploadItem)
        .filter(
            BatchUploadItem.batch_id == batch_id,
            BatchUploadItem.status == BatchUploadItemStatus.PENDING,
        )
        .all()
    )

    cancelled_count = 0
    for item in pending_items:
        item.status = BatchUploadItemStatus.CANCELLED
        cancelled_count += 1

    batch.cancelled_files = cancelled_count

    # Update batch status if all items cancelled
    remaining = (
        db.query(BatchUploadItem)
        .filter(
            BatchUploadItem.batch_id == batch_id,
            BatchUploadItem.status.notin_(
                [
                    BatchUploadItemStatus.CANCELLED,
                    BatchUploadItemStatus.FAILED,
                    BatchUploadItemStatus.COMPLETED,
                ]
            ),
        )
        .count()
    )

    if remaining == 0:
        completed = (
            db.query(BatchUploadItem)
            .filter(
                BatchUploadItem.batch_id == batch_id,
                BatchUploadItem.status == BatchUploadItemStatus.COMPLETED,
            )
            .count()
        )

        if completed > 0:
            batch.status = BatchUploadStatus.PARTIALLY_COMPLETED
        else:
            batch.status = BatchUploadStatus.CANCELLED

    db.commit()

    return {
        "batch_id": batch.id,
        "message": f"Cancelled {cancelled_count} pending items",
        "cancelled_count": cancelled_count,
    }


@scan_router.post("/confirm")
async def confirm_limit(
    user: User = Depends(verify_subscription), db: Session = Depends(get_session)
):
    # Get the active subscription for the current stripe mode
    subscription = user.get_active_subscription()
    if subscription.limit_exceeded:
        raise HTTPException(
            status_code=400, detail="Your confirmation for this month was already set."
        )
    subscription.limit_exceeded = True
    db.commit()


@scan_router.post("/tracks/{url:path}")
async def upload_url(url: str, user: User = Depends(verify_subscription)):
    print("here")
    # the URL is encoded, decode frist
    url_decoded = urllib.parse.unquote(url)
    return acrcloud.uploadUrl(url_decoded, user.acrcloud_container_id)


def _cleanup_deleted_track(file_id: str, stored_file: Optional[str]):
    """Background task to clean up ACRCloud and local file after delete."""
    try:
        acrcloud.deleteFile(file_id)
    except Exception as e:
        logger.warning(f"Failed to delete file {file_id} from ACRCloud: {e}")

    if stored_file and os.path.exists(stored_file):
        try:
            os.remove(stored_file)
        except Exception as e:
            logger.warning(f"Failed to delete stored file {stored_file}: {e}")


@scan_router.delete("/tracks/{file_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_track(
    file_id: str,
    background_tasks: BackgroundTasks,
    user: User = Depends(verify_subscription),
    db: Session = Depends(get_session),
):
    # Direct query instead of loading all user scans
    scan = (
        db.query(ACRCloudScan)
        .filter(
            ACRCloudScan.acrcloud_file_id == file_id,
            ACRCloudScan.user_id == user.id,
        )
        .first()
    )

    if not scan:
        raise HTTPException(status_code=404, detail="The file does not exist.")

    stored_file = scan.stored_file_path

    # Delete from database first (fast operation)
    db.delete(scan)
    db.commit()

    # Clean up ACRCloud and local file in background (slow operations)
    background_tasks.add_task(_cleanup_deleted_track, file_id, stored_file)


@scan_router.put("/tracks/{file_id}", status_code=status.HTTP_200_OK)
async def rescan_track(
    file_id: str,
    background_tasks: BackgroundTasks,
    is_auto: bool = False,
    user: User = Depends(verify_subscription),
    db: Session = Depends(get_session),
):
    """
    Rescan a track - triggers full comprehensive detection again.
    Uses the same detection mechanism as initial upload (Audd, AcoustID, ACRCloud).
    For auto-rescans, creates notification only when a match is found.
    """
    file_ids = [scan.acrcloud_file_id for scan in user.scans]
    if file_id not in file_ids:
        raise HTTPException(status_code=400, detail="The file does not exist.")

    # Get the scan record
    scan = (
        db.query(ACRCloudScan)
        .filter(
            ACRCloudScan.acrcloud_file_id == file_id,
            ACRCloudScan.user_id == user.id,
        )
        .first()
    )

    if not scan:
        raise HTTPException(status_code=404, detail="Scan record not found.")

    # Check if we have a stored file for comprehensive detection
    stored_file = scan.stored_file_path
    if not stored_file or not os.path.exists(stored_file):
        raise HTTPException(
            status_code=400,
            detail="Audio file not available for rescan. This file was uploaded before file storage was enabled.",
        )

    # Get file info for response message
    try:
        file_info = acrcloud.showContainerContents([file_id])
        filename = "rescanned_file.mp3"
        if file_info:
            songs = (
                file_info.get("songs", [])
                if isinstance(file_info, dict)
                else (file_info.songs if hasattr(file_info, "songs") else [])
            )
            if songs and len(songs) > 0:
                song = songs[0]
                filename = (
                    song.get("filename", filename)
                    if isinstance(song, dict)
                    else (song.filename if hasattr(song, "filename") else filename)
                )
    except:
        filename = f"{file_id}.mp3"

    # Extract previous matches BEFORE resetting (for auto-rescan comparison)
    previous_matches = []
    if is_auto and scan.comprehensive_detection:
        prev_detection = scan.comprehensive_detection
        if prev_detection.get("status") == "match_found":
            # Collect all previous match keys
            if prev_detection.get("match"):
                m = prev_detection["match"]
                previous_matches.append(
                    f"{m.get('title', 'Unknown')}|{m.get('artist', 'Unknown')}"
                )
            if prev_detection.get("alternative_matches"):
                for m in prev_detection["alternative_matches"]:
                    previous_matches.append(
                        f"{m.get('title', 'Unknown')}|{m.get('artist', 'Unknown')}"
                    )
        print(
            f"[DEBUG] Extracted {len(previous_matches)} previous matches for comparison: {previous_matches}"
        )

    # Reset comprehensive detection to show "Processing..." state
    scan.comprehensive_detection = None
    scan.is_pending_auto_rescan = is_auto
    flag_modified(scan, "comprehensive_detection")
    db.commit()

    # Run comprehensive detection in background using stored file
    # This uses the EXACT same detection as initial upload
    background_tasks.add_task(
        run_comprehensive_detection_background,
        stored_file,
        file_id,
        user.id,
        is_auto,  # is_auto - affects notification type
        True,  # keep_file=True - don't delete the stored file
        previous_matches,  # Pass previous matches for comparison
    )

    print(f"[INFO] Comprehensive rescan scheduled for {file_id} (is_auto={is_auto})")

    return {"success": True, "message": f"Rescan triggered for {filename}"}


@scan_router.delete(
    "/tracks/{file_id}/matches/{match_index}", status_code=status.HTTP_200_OK
)
async def delete_match(
    file_id: str,
    match_index: int,
    user: User = Depends(verify_subscription),
    db: Session = Depends(get_session),
):
    """Delete a specific match from a scan's results by index"""
    # Verify the file belongs to this user
    scan = (
        db.query(ACRCloudScan)
        .filter(
            ACRCloudScan.acrcloud_file_id == file_id,
            ACRCloudScan.user_id == user.id,
        )
        .first()
    )

    if not scan:
        raise HTTPException(status_code=404, detail="Scan not found.")

    if not scan.comprehensive_detection:
        raise HTTPException(
            status_code=400, detail="No detection results to delete from."
        )

    detection = scan.comprehensive_detection

    if detection.get("status") != "match_found":
        raise HTTPException(status_code=400, detail="No matches to delete.")

    # Build list of all matches (primary + alternatives)
    all_matches = []
    if detection.get("match"):
        all_matches.append(detection["match"])
    if detection.get("alternative_matches"):
        all_matches.extend(detection["alternative_matches"])

    if match_index < 0 or match_index >= len(all_matches):
        raise HTTPException(status_code=400, detail="Invalid match index.")

    # Remove the match at the specified index
    all_matches.pop(match_index)

    # Update the detection structure
    if len(all_matches) == 0:
        # No matches left, set status to no_match
        detection["status"] = "no_match"
        detection["match"] = None
        detection["alternative_matches"] = []
    else:
        # First remaining match becomes the primary
        detection["match"] = all_matches[0]
        detection["alternative_matches"] = (
            all_matches[1:] if len(all_matches) > 1 else []
        )

    # Save back to database - reassign and flag as modified for SQLAlchemy to detect the change
    scan.comprehensive_detection = detection
    flag_modified(scan, "comprehensive_detection")
    db.commit()

    return {"success": True, "remaining_matches": len(all_matches)}


@scan_router.post("/trigger-auto-rescan", status_code=status.HTTP_200_OK)
async def trigger_auto_rescan(
    background_tasks: BackgroundTasks,
    user: User = Depends(verify_subscription),
    db: Session = Depends(get_session),
):
    """
    Development-only endpoint to trigger auto-rescan for all enabled scans.
    This simulates what would happen when 14 days have passed.
    Runs FULL comprehensive detection (same as initial upload).
    Notification only created when match is found.
    """
    settings = get_settings()
    if settings.mode != "development":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="This endpoint is only available in development mode",
        )

    # Get all scans for this user
    scans = db.query(ACRCloudScan).filter(ACRCloudScan.user_id == user.id).all()

    if not scans:
        return {"success": False, "message": "No scans found for user"}

    triggered_count = 0
    skipped_count = 0

    for scan in scans:
        try:
            # Check if we have a stored file
            stored_file = scan.stored_file_path
            if not stored_file or not os.path.exists(stored_file):
                logger.warning(f"Skipping {scan.acrcloud_file_id} - no stored file")
                skipped_count += 1
                continue

            # Extract previous matches BEFORE resetting (for comparison)
            previous_matches = []
            if scan.comprehensive_detection:
                prev_detection = scan.comprehensive_detection
                if prev_detection.get("status") == "match_found":
                    if prev_detection.get("match"):
                        m = prev_detection["match"]
                        previous_matches.append(
                            f"{m.get('title', 'Unknown')}|{m.get('artist', 'Unknown')}"
                        )
                    if prev_detection.get("alternative_matches"):
                        for m in prev_detection["alternative_matches"]:
                            previous_matches.append(
                                f"{m.get('title', 'Unknown')}|{m.get('artist', 'Unknown')}"
                            )

            # Reset comprehensive detection to show "Processing..." state
            scan.comprehensive_detection = None
            scan.is_pending_auto_rescan = True
            flag_modified(scan, "comprehensive_detection")

            # Schedule comprehensive detection in background
            # Uses stored file with SAME detection as initial upload
            background_tasks.add_task(
                run_comprehensive_detection_background,
                stored_file,
                scan.acrcloud_file_id,
                user.id,
                True,  # is_auto=True - will create "New match found (auto-scan)" notification
                True,  # keep_file=True - don't delete stored file
                previous_matches,  # Pass previous matches for comparison
            )

            triggered_count += 1
            print(
                f"[INFO] Auto-rescan scheduled for {scan.acrcloud_file_id} (prev_matches: {len(previous_matches)})"
            )

        except Exception as e:
            logger.error(
                f"Error triggering auto-rescan for {scan.acrcloud_file_id}: {e}"
            )

    db.commit()

    return {
        "success": True,
        "message": f"Auto-rescan triggered for {triggered_count} scans",
        "triggered_count": triggered_count,
        "skipped_no_file": skipped_count,
        "total_scans": len(scans),
    }
