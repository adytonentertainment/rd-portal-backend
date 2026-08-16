"""
MLC Catalog Audit Endpoint

Performs an audit of user's catalog against MLC registration data
"""
from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks
from sqlalchemy.orm import Session
from typing import Dict, Any, List, Optional
import logging
import re
import requests
import unicodedata
from datetime import datetime

from app.database.session import get_session, SessionLocal
from app.models.models import User, UserCatalog, Songs, RevenueTransaction, StatsCache, Client
from app.routers.auth import get_user
from app.libs.MLC.mlc_api import MLCClient
from app.settings.settings import get_settings
from app.services.notification_service import NotificationService
from app.crud.stats import PUBLISHING_ROYALTY_PER_STREAM, YOUTUBE_PUBLISHING_ROYALTY_PER_VIEW

logger = logging.getLogger(__name__)


def ipi_matches(user_ipi: str, mlc_ipi: str) -> bool:
    """
    Compare two IPI numbers with leading zero tolerance.
    MLC stores IPIs with leading zeros (e.g., "00844814814" vs "844814814").

    Args:
        user_ipi: User's IPI number
        mlc_ipi: IPI from MLC database

    Returns:
        True if IPIs match (ignoring any leading zeros)
    """
    if not user_ipi or not mlc_ipi:
        return False

    user_clean = str(user_ipi).strip()
    mlc_clean = str(mlc_ipi).strip()

    # Direct match
    if user_clean == mlc_clean:
        return True

    # Strip ALL leading zeros from both and compare
    user_stripped = user_clean.lstrip('0')
    mlc_stripped = mlc_clean.lstrip('0')

    return user_stripped == mlc_stripped


mlc_audit_router = APIRouter(prefix="/mlc-audit", tags=["MLC Audit"])


def send_audit_report_email(
    user_email: str,
    user_name: str,
    audit_results: List[Dict[str, Any]]
) -> bool:
    """
    Send MLC audit report via Formspree in plain text format

    Args:
        user_email: User's email address
        user_name: User's full name
        audit_results: List of audit results for each track

    Returns:
        True if email sent successfully, False otherwise
    """
    try:
        # Calculate statistics
        total_tracks = len(audit_results)
        registered_tracks = [r for r in audit_results if r.get('registered_with_user')]
        unregistered_tracks = [r for r in audit_results if not r.get('registered_with_user')]

        registered_count = len(registered_tracks)
        unregistered_count = len(unregistered_tracks)

        # Generate detailed plain text report
        report_lines = []
        report_lines.append("=" * 80)
        report_lines.append("MECHANICAL LICENSING COLLECTIVE (MLC) CATALOG AUDIT REPORT")
        report_lines.append("=" * 80)
        report_lines.append("")
        report_lines.append(f"Report Date: {datetime.now().strftime('%B %d, %Y at %I:%M %p UTC')}")
        report_lines.append(f"Rights Holder: {user_name}")
        report_lines.append(f"Contact Email: {user_email}")
        report_lines.append("")
        report_lines.append("-" * 80)
        report_lines.append("EXECUTIVE SUMMARY")
        report_lines.append("-" * 80)
        report_lines.append("")
        report_lines.append(f"Total Musical Works Audited: {total_tracks}")
        report_lines.append(f"Works Registered with MLC: {registered_count} ({registered_count/total_tracks*100:.1f}%)")
        report_lines.append(f"Works Not Found in MLC Database: {unregistered_count} ({unregistered_count/total_tracks*100:.1f}%)")
        report_lines.append("")

        if registered_count == total_tracks:
            report_lines.append("STATUS: COMPLIANT - All musical works are properly registered with the MLC.")
            report_lines.append("You should be receiving mechanical royalties for all eligible digital streams")
            report_lines.append("and downloads from streaming services including Spotify, Apple Music, Amazon")
            report_lines.append("Music, YouTube Music, and other digital service providers.")
        elif unregistered_count > 0:
            report_lines.append("STATUS: ACTION REQUIRED - Unregistered works detected.")
            report_lines.append("Some of your musical works are not registered with the MLC, which may result")
            report_lines.append("in unclaimed mechanical royalties. Please review the unregistered works section")
            report_lines.append("below and take appropriate action to register these works.")

        report_lines.append("")

        # Registered Tracks Section
        if registered_tracks:
            report_lines.append("=" * 80)
            report_lines.append(f"SECTION 1: REGISTERED MUSICAL WORKS ({registered_count} works)")
            report_lines.append("=" * 80)
            report_lines.append("")
            report_lines.append("The following musical works have been successfully located in the MLC public")
            report_lines.append("database and are registered under your name as either a writer or publisher.")
            report_lines.append("You should be receiving mechanical royalty payments for these works through")
            report_lines.append("the MLC's blanket licensing system.")
            report_lines.append("")

            for i, track in enumerate(registered_tracks, 1):
                report_lines.append(f"{i}. MUSICAL WORK: {track['title']}")
                report_lines.append(f"   Recording Artist: {track['artist']}")
                report_lines.append(f"   Registration Status: REGISTERED WITH MLC")
                report_lines.append("")

                if track.get('works'):
                    report_lines.append("   MLC Work Registration Details:")
                    for work in track['works']:
                        if work.get('user_is_writer') or work.get('user_is_publisher'):
                            work_title = work.get('title', 'Unknown Title')
                            roles = []
                            if work.get('user_is_writer'):
                                roles.append("Songwriter/Composer")
                            if work.get('user_is_publisher'):
                                roles.append("Music Publisher")

                            report_lines.append(f"   - Work Title: \"{work_title}\"")
                            report_lines.append(f"     Your Role: {', '.join(roles)}")
                            report_lines.append(f"     Rights Holder Confirmation: {user_name}")
                    report_lines.append("")

                report_lines.append("   Legal Implications:")
                report_lines.append("   - You are entitled to receive mechanical royalties for this work")
                report_lines.append("   - The MLC will collect and distribute royalties on your behalf")
                report_lines.append("   - Payments are typically made on a quarterly basis")
                report_lines.append("   - Ensure your payment information is current in your MLC account")
                report_lines.append("")
                report_lines.append("-" * 80)
                report_lines.append("")

        # Unregistered Tracks Section
        if unregistered_tracks:
            report_lines.append("=" * 80)
            report_lines.append(f"SECTION 2: UNREGISTERED MUSICAL WORKS ({unregistered_count} works)")
            report_lines.append("=" * 80)
            report_lines.append("")
            report_lines.append("**IMPORTANT NOTICE**")
            report_lines.append("")
            report_lines.append("The following musical works were NOT found in the MLC public database under")
            report_lines.append("your registered name. This means you may NOT be receiving mechanical royalties")
            report_lines.append("for digital streams and downloads of these works.")
            report_lines.append("")
            report_lines.append("POTENTIAL FINANCIAL IMPACT:")
            report_lines.append("Unregistered works may result in unclaimed royalties that are held in the MLC's")
            report_lines.append("unmatched royalty pool. According to the Music Modernization Act (MMA), these")
            report_lines.append("royalties may become unavailable for claim after the statutory claiming period.")
            report_lines.append("")

            for i, track in enumerate(unregistered_tracks, 1):
                report_lines.append(f"{i}. MUSICAL WORK: {track['title']}")
                report_lines.append(f"   Recording Artist: {track['artist']}")
                report_lines.append(f"   Registration Status: NOT FOUND IN MLC DATABASE")
                report_lines.append("")

                if track.get('note'):
                    report_lines.append(f"   Additional Information:")
                    report_lines.append(f"   {track['note']}")
                    report_lines.append("")

                report_lines.append("   Required Actions:")
                report_lines.append("   1. Verify you own mechanical rights to this musical work")
                report_lines.append("   2. Register the work with the MLC at https://www.themlc.com")
                report_lines.append("   3. Ensure accurate songwriter and publisher information")
                report_lines.append("   4. Provide your IPI/CAE number if available")
                report_lines.append("   5. Allow 4-6 weeks for registration processing")
                report_lines.append("")
                report_lines.append("   Legal Considerations:")
                report_lines.append("   - Failure to register may result in lost royalty revenue")
                report_lines.append("   - The MLC cannot pay royalties for unregistered works")
                report_lines.append("   - Historical royalties may be available upon registration")
                report_lines.append("   - Contact the MLC directly for questions about claiming past royalties")
                report_lines.append("")
                report_lines.append("-" * 80)
                report_lines.append("")

        # Recommendations Section
        report_lines.append("=" * 80)
        report_lines.append("SECTION 3: RECOMMENDATIONS AND NEXT STEPS")
        report_lines.append("=" * 80)
        report_lines.append("")

        if unregistered_count > 0:
            report_lines.append("IMMEDIATE ACTION REQUIRED:")
            report_lines.append("")
            report_lines.append(f"You have {unregistered_count} musical work(s) that require registration with the MLC.")
            report_lines.append("")
            report_lines.append("STEP-BY-STEP REGISTRATION PROCESS:")
            report_lines.append("")
            report_lines.append("1. CREATE OR LOG INTO YOUR MLC ACCOUNT")
            report_lines.append("   - Visit: https://www.themlc.com")
            report_lines.append("   - Click \"Member Portal\" or \"Sign In\"")
            report_lines.append("   - If you don't have an account, select \"Create Account\"")
            report_lines.append("")
            report_lines.append("2. GATHER REQUIRED INFORMATION")
            report_lines.append("   - Song title and any alternate titles")
            report_lines.append("   - All songwriters and their ownership percentages")
            report_lines.append("   - All music publishers and their shares")
            report_lines.append("   - ISWC (International Standard Musical Work Code) if available")
            report_lines.append("   - IPI/CAE numbers for all writers and publishers")
            report_lines.append("")
            report_lines.append("3. SUBMIT MUSICAL WORK REGISTRATIONS")
            report_lines.append("   - Navigate to the \"Register Works\" section")
            report_lines.append("   - Complete all required fields accurately")
            report_lines.append("   - Upload supporting documentation if requested")
            report_lines.append("   - Submit each work for processing")
            report_lines.append("")
            report_lines.append("4. VERIFY AND MONITOR")
            report_lines.append("   - Check registration status after 4-6 weeks")
            report_lines.append("   - Update any changes to ownership or publisher information")
            report_lines.append("   - Review quarterly royalty statements")
            report_lines.append("")
            report_lines.append("IMPORTANT LEGAL NOTES:")
            report_lines.append("- Only register works for which you own or control mechanical rights")
            report_lines.append("- Providing false information may result in account suspension")
            report_lines.append("- Disputes about ownership should be resolved before registration")
            report_lines.append("- The MLC operates under the Music Modernization Act (17 U.S.C. § 115)")
            report_lines.append("")
        else:
            report_lines.append("COMPLIANCE STATUS: EXCELLENT")
            report_lines.append("")
            report_lines.append("All of your musical works are properly registered with the Mechanical")
            report_lines.append("Licensing Collective. You should be receiving mechanical royalty payments")
            report_lines.append("for all eligible digital streams and downloads.")
            report_lines.append("")
            report_lines.append("ONGOING BEST PRACTICES:")
            report_lines.append("- Register new musical works within 30 days of commercial release")
            report_lines.append("- Keep your contact and payment information up to date")
            report_lines.append("- Review quarterly royalty statements for accuracy")
            report_lines.append("- Report any discrepancies to the MLC within 90 days")
            report_lines.append("- Maintain accurate records of songwriting credits and ownership splits")
            report_lines.append("")

        report_lines.append("=" * 80)
        report_lines.append("ADDITIONAL RESOURCES")
        report_lines.append("=" * 80)
        report_lines.append("")
        report_lines.append("The Mechanical Licensing Collective (MLC)")
        report_lines.append("Website: https://www.themlc.com")
        report_lines.append("Member Portal: https://portal.themlc.com")
        report_lines.append("Support Email: info@themlc.com")
        report_lines.append("Support Phone: 1-844-MLC-INFO (1-844-652-4636)")
        report_lines.append("")
        report_lines.append("Educational Resources:")
        report_lines.append("- About Mechanical Royalties: https://www.themlc.com/music-users")
        report_lines.append("- Frequently Asked Questions: https://www.themlc.com/resources/faqs")
        report_lines.append("- Music Modernization Act Overview: https://www.copyright.gov/music-modernization/")
        report_lines.append("")
        report_lines.append("TuneScan Resources:")
        report_lines.append("- Your Catalog Dashboard: https://development.tunescan.app/catalog")
        report_lines.append("- Account Settings: https://development.tunescan.app/settings")
        report_lines.append("- Support: support@tunescan.app")
        report_lines.append("")
        report_lines.append("=" * 80)
        report_lines.append("DISCLAIMER")
        report_lines.append("=" * 80)
        report_lines.append("")
        report_lines.append("This report is provided for informational purposes only and does not constitute")
        report_lines.append("legal or financial advice. The information contained herein is based on publicly")
        report_lines.append("available MLC registration data and may not reflect recent changes or pending")
        report_lines.append("registrations. TuneScan makes no warranties about the accuracy or completeness")
        report_lines.append("of this information.")
        report_lines.append("")
        report_lines.append("For legal questions regarding mechanical rights, royalty collection, or the")
        report_lines.append("registration process, please consult with a qualified music attorney or contact")
        report_lines.append("the MLC directly.")
        report_lines.append("")
        report_lines.append("This audit was performed using the MLC's public API. Registration status is")
        report_lines.append("current as of the report generation date. Ongoing monitoring is recommended")
        report_lines.append("to ensure continued compliance and royalty collection.")
        report_lines.append("")
        report_lines.append("=" * 80)
        report_lines.append("END OF REPORT")
        report_lines.append("=" * 80)
        report_lines.append("")
        report_lines.append(f"Generated by TuneScan Music Rights Management Platform")
        report_lines.append(f"Report ID: MLC-AUDIT-{datetime.now().strftime('%Y%m%d-%H%M%S')}")
        report_lines.append("")

        message_body = "\n".join(report_lines)

        # Send via Formspree
        formspree_url = "https://formspree.io/f/myznqnnn"

        payload = {
            "email": user_email,
            "subject": f"MLC Catalog Audit Report - {registered_count}/{total_tracks} Tracks Registered",
            "message": message_body,
            "_replyto": user_email,
            "_subject": f"MLC Catalog Audit Report - {registered_count}/{total_tracks} Tracks Registered"
        }

        print(f"[MLC AUDIT] Sending email to {user_email}")

        response = requests.post(
            formspree_url,
            data=payload,
            headers={"Accept": "application/json"}
        )

        if response.status_code == 200:
            print(f"[MLC AUDIT] ✅ Email sent successfully to {user_email}")
            logger.info(f"MLC audit email sent to {user_email}")
            return True
        else:
            print(f"[MLC AUDIT] ❌ Failed to send email. Status: {response.status_code}, Response: {response.text}")
            logger.error(f"Failed to send MLC audit email: {response.status_code} - {response.text}")
            return False

    except Exception as e:
        print(f"[MLC AUDIT] ❌ Exception sending email: {e}")
        logger.error(f"Exception sending MLC audit email: {e}")
        import traceback
        traceback.print_exc()
        return False


def perform_mlc_audit_task(
    user_id: int,
    first_name: str,
    last_name: str,
    user_email: str,
    client_id: int = None,
):
    """
    Background task to perform MLC audit

    This will:
    1. Fetch all user catalog tracks
    2. Query MLC API for each track by title and writer name
    3. Check if user's name matches any writer/publisher
    4. Generate audit report
    5. Send email with results via Formspree
    """
    # Create a new database session for background task
    db = SessionLocal()

    try:
        import sys
        sys.stderr.write(f"[MLC AUDIT] Starting MLC audit for user {user_id}\n")
        sys.stderr.flush()
        print(f"[MLC AUDIT] Starting MLC audit for user {user_id}", flush=True)
        logger.info(f"Starting MLC audit for user {user_id}")

        # Get user catalog
        query = db.query(UserCatalog).join(
            Songs, UserCatalog.song_id == Songs.id
        ).filter(UserCatalog.user_id == user_id)
        if client_id is not None:
            query = query.filter(UserCatalog.client_id == client_id)
        catalog_entries = query.all()

        if not catalog_entries:
            print(f"[MLC AUDIT] No catalog found for user {user_id}")
            logger.warning(f"No catalog found for user {user_id}")
            return

        print(f"[MLC AUDIT] Found {len(catalog_entries)} tracks in catalog")
        logger.info(f"Found {len(catalog_entries)} tracks in catalog")

        # Initialize MLC client with credentials from settings
        try:
            settings = get_settings()
            mlc_client = MLCClient(
                username=settings.mlc_username,
                password=settings.mlc_password,
                api_url=settings.mlc_api_url
            )
        except ValueError as e:
            logger.error(f"Failed to initialize MLC client: {e}")
            return

        audit_results = []
        full_name = f"{first_name} {last_name}".strip()

        # Get user's IPI numbers for matching
        user_obj = db.query(User).filter(User.id == user_id).first()
        user_writer_ipi = getattr(user_obj, 'writer_ipi', None) or getattr(user_obj, 'ipi_number', None)
        user_publisher_ipi = getattr(user_obj, 'publisher_ipi', None)

        for entry in catalog_entries:
            song = entry.song

            try:
                # Search MLC recordings by artist and title
                print(f"[MLC AUDIT] Searching MLC for: {song.title} by {song.artist}")
                logger.info(f"Searching MLC for: {song.title} by {song.artist}")

                # Step 1: Search for recordings by artist and title
                recordings = mlc_client.search_recordings(
                    artist=song.artist,
                    title=song.title
                )

                track_result = {
                    'track_id': song.spotify_track_id,
                    'title': song.title,
                    'artist': song.artist,
                    'isrc': song.isrc,
                    'mlc_recordings_found': len(recordings) if isinstance(recordings, list) else 0,
                    'registered_with_user': False,
                    'works': []
                }

                if not recordings or not isinstance(recordings, list):
                    track_result['note'] = 'No recordings found in MLC'
                    audit_results.append(track_result)
                    continue

                # Step 2: For each recording, get the full work details
                for recording in recordings:
                    mlc_song_code = recording.get('mlcsongCode') or recording.get('mlcSongCode')

                    if not mlc_song_code:
                        continue

                    try:
                        # Get full work details
                        work = mlc_client.get_work_by_id(mlc_song_code)

                        if not work:
                            continue

                        writers = work.get('writers', [])
                        publishers = work.get('publishers', [])

                        # Check if user is listed as writer (by name OR IPI)
                        user_is_writer = False
                        for writer in writers:
                            writer_first = writer.get('writerFirstName', '').lower()
                            writer_last = writer.get('writerLastName', '').lower()
                            writer_full = f"{writer_first} {writer_last}".strip()
                            writer_ipi = writer.get('ipiNumber') or writer.get('writerIpiNumber') or writer.get('ipi', '')

                            # Match by name
                            if first_name.lower() in writer_full and last_name.lower() in writer_full:
                                user_is_writer = True
                                break
                            # Match by IPI
                            if user_writer_ipi and writer_ipi and ipi_matches(user_writer_ipi, str(writer_ipi)):
                                user_is_writer = True
                                break

                        # Check if user is listed as publisher (by name OR IPI)
                        user_is_publisher = False
                        for pub in publishers:
                            pub_name = pub.get('publisherName', '').lower()
                            pub_ipi = pub.get('ipiNumber') or pub.get('publisherIpiNumber') or pub.get('ipi', '')

                            # Match by name
                            if first_name.lower() in pub_name and last_name.lower() in pub_name:
                                user_is_publisher = True
                                break
                            # Match by IPI
                            if user_publisher_ipi and pub_ipi and ipi_matches(user_publisher_ipi, str(pub_ipi)):
                                user_is_publisher = True
                                break

                        if user_is_writer or user_is_publisher:
                            track_result['registered_with_user'] = True

                        track_result['works'].append({
                            'mlcSongCode': mlc_song_code,
                            'title': work.get('primaryTitle') or work.get('title'),
                            'iswc': work.get('iswc'),
                            'writers': [
                                f"{w.get('writerFirstName', '')} {w.get('writerLastName', '')}".strip()
                                for w in writers
                            ],
                            'publishers': [p.get('publisherName') for p in publishers],
                            'user_is_writer': user_is_writer,
                            'user_is_publisher': user_is_publisher
                        })

                    except Exception as e:
                        logger.warning(f"Error fetching work {mlc_song_code}: {e}")
                        continue

                audit_results.append(track_result)

            except Exception as e:
                logger.error(f"Error auditing track {song.title}: {e}")
                audit_results.append({
                    'track_id': song.spotify_track_id,
                    'title': song.title,
                    'artist': song.artist,
                    'error': str(e)
                })

        # Store audit results (you could save this to database or email to user)
        print(f"[MLC AUDIT] Completed MLC audit for user {user_id}")
        print(f"[MLC AUDIT] Total tracks audited: {len(audit_results)}")
        logger.info(f"Completed MLC audit for user {user_id}")
        logger.info(f"Total tracks audited: {len(audit_results)}")

        # Count tracks with MLC registrations
        registered_count = sum(1 for r in audit_results if r.get('registered_with_user'))
        print(f"[MLC AUDIT] Tracks registered with user's name: {registered_count}")
        logger.info(f"Tracks registered with user's name: {registered_count}")

        # Send email with audit report
        user_name = f"{first_name} {last_name}"
        email_sent = send_audit_report_email(user_email, user_name, audit_results)

        if email_sent:
            print(f"[MLC AUDIT] ✅ Audit report emailed successfully to {user_email}")
        else:
            print(f"[MLC AUDIT] ⚠️ Audit completed but email delivery failed")

        # TODO: Store audit report in database for future reference

        return audit_results

    except Exception as e:
        print(f"[MLC AUDIT] EXCEPTION: Failed to perform MLC audit: {e}")
        logger.error(f"Failed to perform MLC audit: {e}")
        import traceback
        traceback.print_exc()
        raise
    finally:
        db.close()


@mlc_audit_router.post("/request")
async def request_mlc_audit(
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_session),
    user: User = Depends(get_user),
    client_id: Optional[int] = None,
):
    """
    Request an MLC catalog audit

    This will:
    1. Get user's first and last name from User model
    2. Start a background task to audit the catalog
    3. Return confirmation message
    """
    if not user:
        raise HTTPException(status_code=401, detail="Authentication required")

    # Get user's name from User model (assuming these fields exist)
    first_name = getattr(user, 'first_name', None)
    last_name = getattr(user, 'last_name', None)

    if not first_name or not last_name:
        raise HTTPException(
            status_code=400,
            detail="Please set your first and last name in Settings before requesting an audit"
        )

    # Get user's email
    user_email = getattr(user, 'email', None)
    if not user_email:
        raise HTTPException(
            status_code=400,
            detail="User email not found"
        )

    # Start background task (don't pass db, it will create its own session)
    background_tasks.add_task(
        perform_mlc_audit_task,
        user.id,
        first_name,
        last_name,
        user_email,
        client_id,
    )

    return {
        "message": "MLC audit request received",
        "status": "processing",
        "user": f"{first_name} {last_name}",
        "estimated_completion": "24 hours"
    }


@mlc_audit_router.get("/status")
async def get_audit_status(
    db: Session = Depends(get_session),
    user: User = Depends(get_user)
):
    """
    Get status of user's MLC audit

    TODO: Implement audit status tracking in database
    """
    if not user:
        raise HTTPException(status_code=401, detail="Authentication required")

    # TODO: Query database for audit status
    return {
        "status": "No audit in progress",
        "last_audit": None
    }


@mlc_audit_router.get("/catalog-check")
async def check_catalog_mlc_registration(
    db: Session = Depends(get_session),
    user: User = Depends(get_user),
    client_id: Optional[int] = None,
) -> Dict[str, Any]:
    """
    Check MLC registration status for all tracks in user's catalog.

    This endpoint:
    1. Fetches all tracks from the user's catalog
    2. Queries MLC API for each track by ISRC (or title/artist)
    3. Returns registration status, writers, IPI numbers, and whether user matches

    The response includes:
    - tracks: List of track check results with MLC data
    - summary: Overall statistics (registered, unregistered, user_matches)
    """
    if not user:
        raise HTTPException(status_code=401, detail="Authentication required")

    # Get IPI numbers and names for matching
    # If a client is selected, use client's IPI/name data; otherwise use user's
    client = None
    if client_id is not None:
        client = db.query(Client).filter(
            Client.id == client_id, Client.user_id == user.id
        ).first()

    if client and (client.writer_ipi or client.writer_name or client.publisher_ipi or client.publisher_name):
        # Use client-level IPI/name data
        user_writer_ipi = client.writer_ipi
        user_publisher_ipi = client.publisher_ipi
        user_ipi = user_writer_ipi or getattr(user, 'ipi_number', None)
        user_first_name = (client.writer_name or '').split(' ')[0] if client.writer_name else (getattr(user, 'first_name', '') or '')
        user_last_name = ' '.join((client.writer_name or '').split(' ')[1:]) if client.writer_name and ' ' in client.writer_name else (getattr(user, 'last_name', '') or '')
        user_full_name = (client.writer_name or '').strip().lower() or f"{user_first_name} {user_last_name}".strip().lower()
        user_publisher_name = client.publisher_name or ''
        logger.info(f"[MLC AUDIT] Using CLIENT data (id={client_id}): writer_ipi='{user_writer_ipi}', publisher_ipi='{user_publisher_ipi}', writer_name='{client.writer_name}', publisher_name='{user_publisher_name}'")
    else:
        # Fall back to user-level IPI/name data
        user_writer_ipi = getattr(user, 'writer_ipi', None)
        user_publisher_ipi = getattr(user, 'publisher_ipi', None)
        user_ipi = user_writer_ipi or getattr(user, 'ipi_number', None)  # Prefer writer_ipi
        user_first_name = getattr(user, 'first_name', '') or ''
        user_last_name = getattr(user, 'last_name', '') or ''
        user_full_name = f"{user_first_name} {user_last_name}".strip().lower()
        user_publisher_name = getattr(user, 'publisher_name', '') or ''
        logger.info(f"[MLC AUDIT] Using USER data: writer_ipi='{user_writer_ipi}', publisher_ipi='{user_publisher_ipi}', legacy_ipi='{getattr(user, 'ipi_number', None)}'")
        logger.info(f"[MLC AUDIT] User name: '{user_full_name}', publisher_name: '{user_publisher_name}'")

    # Get user's catalog
    catalog_query = db.query(UserCatalog).join(
        Songs, UserCatalog.song_id == Songs.id
    ).filter(UserCatalog.user_id == user.id)
    if client_id is not None:
        catalog_query = catalog_query.filter(UserCatalog.client_id == client_id)
    catalog_entries = catalog_query.all()

    if not catalog_entries:
        return {
            "tracks": [],
            "summary": {
                "total": 0,
                "mlc_registered": 0,
                "not_found": 0,
                "user_matches": 0,
                "ipi_matches": 0
            }
        }

    # Initialize MLC client
    try:
        settings = get_settings()
        mlc_client = MLCClient(
            username=settings.mlc_username,
            password=settings.mlc_password,
            api_url=settings.mlc_api_url
        )
    except ValueError as e:
        logger.error(f"Failed to initialize MLC client: {e}")
        raise HTTPException(status_code=500, detail="MLC API configuration error")
    except Exception as e:
        logger.error(f"Failed to initialize MLC client: {e}")
        raise HTTPException(status_code=500, detail="Failed to connect to MLC API")

    tracks_result = []
    total_registered = 0
    total_not_found = 0
    total_user_matches = 0
    total_ipi_matches = 0

    for entry in catalog_entries:
        song = entry.song
        track_result = {
            "id": song.id,
            "spotify_track_id": song.spotify_track_id,
            "title": song.title,
            "artist": song.artist,
            "isrc": song.isrc,
            "publishing_royalty": entry.publishing_royalty or 0,
            "mlc_registered": False,
            "writers": [],
            "ipi_numbers": [],
            "publishers": [],
            "user_is_writer": False,
            "user_is_publisher": False,
            "user_matched_by": None,  # 'name', 'ipi', or 'both' (legacy, for backwards compat)
            "writer_matched_by": None,  # 'name', 'ipi', or 'both'
            "publisher_matched_by": None,  # 'name', 'ipi', or 'both'
            "mlc_song_code": None,
            "iswc": None,
            "isrc_missing": False,  # True if song found in MLC but ISRC not linked
            "error": None
        }
        found_by_isrc = False

        try:
            # Try to search by ISRC first (most reliable)
            mlc_data = None
            if song.isrc and song.isrc.upper() != 'N/A':
                try:
                    mlc_data = mlc_client.get_complete_work_info_by_isrc(song.isrc)
                    if mlc_data and mlc_data.get('work'):
                        found_by_isrc = True
                except Exception as e:
                    logger.warning(f"ISRC search failed for {song.isrc}: {e}")

            # Verify ISRC-found work actually matches the song title
            # ISRCs can map to different works (e.g., remixes, re-releases)
            if mlc_data and mlc_data.get('work') and found_by_isrc:
                isrc_title = (mlc_data['work'].get('primaryTitle') or '').lower().strip()
                song_title_lower = (song.title or '').lower().strip()
                # Also check if user's writer/publisher is on this work
                isrc_work_writers = [
                    f"{w.get('writerFirstName', '')} {w.get('writerLastName', '')}".strip().lower()
                    for w in mlc_data['work'].get('writers', [])
                ]
                user_on_isrc_work = False
                if user_full_name:
                    for wn in isrc_work_writers:
                        if user_full_name in wn or wn in user_full_name:
                            user_on_isrc_work = True
                            break
                if user_writer_ipi:
                    for w in mlc_data['work'].get('writers', []):
                        w_ipi = w.get('writerIPI') or w.get('ipiNumber') or ''
                        if w_ipi and ipi_matches(user_writer_ipi, w_ipi):
                            user_on_isrc_work = True
                            break

                if song_title_lower not in isrc_title and isrc_title not in song_title_lower and not user_on_isrc_work:
                    logger.warning(f"[MLC AUDIT] ISRC {song.isrc} maps to '{isrc_title}' but song is '{song.title}' and user not found on work — trying title search")
                    isrc_mlc_data = mlc_data  # Keep for reference
                    mlc_data = None
                    found_by_isrc = False

            # Helper: check if user's writer/publisher is on a work
            def user_on_work(work_data):
                if not work_data:
                    return False
                for w in work_data.get('writers', []):
                    wn = f"{w.get('writerFirstName', '')} {w.get('writerLastName', '')}".strip().lower()
                    w_ipi = w.get('writerIPI') or w.get('ipiNumber') or ''
                    if user_full_name and (user_full_name in wn or wn in user_full_name):
                        return True
                    if user_writer_ipi and w_ipi and ipi_matches(user_writer_ipi, w_ipi):
                        return True
                all_pubs = []
                def _collect(pubs):
                    for p in pubs:
                        all_pubs.append(p)
                        if p.get('parentPublishers'):
                            _collect(p['parentPublishers'])
                _collect(work_data.get('publishers', []))
                for p in all_pubs:
                    pn = (p.get('publisherName') or '').lower()
                    p_ipi = p.get('publisherIpiNumber') or p.get('publisherIPI') or ''
                    if user_publisher_name and user_publisher_name.lower() in pn:
                        return True
                    if user_publisher_ipi and p_ipi and ipi_matches(user_publisher_ipi, p_ipi):
                        return True
                return False

            # If ISRC search failed, try by title/artist
            if not mlc_data or not mlc_data.get('work'):
                try:
                    recordings = mlc_client.search_recordings(
                        artist=song.artist,
                        title=song.title
                    )

                    if recordings and isinstance(recordings, list) and len(recordings) > 0:
                        recording = recordings[0]
                        mlc_song_code = recording.get('mlcsongCode') or recording.get('mlcSongCode')

                        if mlc_song_code:
                            work = mlc_client.get_work_by_id(mlc_song_code)
                            mlc_data = {
                                'recording': recording,
                                'work': work,
                                'mlc_song_code': mlc_song_code
                            }
                except Exception as e:
                    logger.warning(f"Title/artist search failed for {song.title}: {e}")

            # If still not found and we have a writer name, retry with writer name as artist
            # MLC may register under the legal/writer name rather than the artist/stage name
            if (not mlc_data or not mlc_data.get('work')) and user_full_name:
                writer_artist = user_full_name.strip()
                if writer_artist and writer_artist.lower() != (song.artist or '').lower():
                    try:
                        logger.info(f"Retrying MLC search for '{song.title}' with writer name '{writer_artist}' as artist")
                        recordings = mlc_client.search_recordings(
                            artist=writer_artist,
                            title=song.title
                        )

                        if recordings and isinstance(recordings, list) and len(recordings) > 0:
                            recording = recordings[0]
                            mlc_song_code = recording.get('mlcsongCode') or recording.get('mlcSongCode')

                            if mlc_song_code:
                                work = mlc_client.get_work_by_id(mlc_song_code)
                                mlc_data = {
                                    'recording': recording,
                                    'work': work,
                                    'mlc_song_code': mlc_song_code
                                }
                                logger.info(f"Found '{song.title}' in MLC using writer name '{writer_artist}'")
                    except Exception as e:
                        logger.warning(f"Writer-name search failed for {song.title}: {e}")

            # If we found a work but user isn't on it, try title+writer search
            # Common with generic titles (e.g., "Hot Girl") where multiple works share the same name
            if mlc_data and mlc_data.get('work') and not user_on_work(mlc_data['work']) and (user_writer_ipi or user_full_name):
                logger.info(f"[MLC AUDIT] Found '{song.title}' but user not on work — trying title+writer search")
                fallback_mlc_data = mlc_data  # Preserve in case title+writer search fails

            # Search works database by title + writer IPI (always try if user not matched yet)
            if (not mlc_data or not mlc_data.get('work') or (mlc_data.get('work') and not user_on_work(mlc_data['work']))) and (user_writer_ipi or user_full_name):
                try:
                    logger.info(f"Searching MLC works for '{song.title}' by title + writer IPI/name")
                    works = mlc_client.search_by_title_and_writer(
                        title=song.title,
                        writer_name=user_full_name if user_full_name else None,
                        writer_ipi=user_writer_ipi if user_writer_ipi else None,
                    )

                    if works and isinstance(works, list) and len(works) > 0:
                        work_result = works[0]
                        mlc_song_code = work_result.get('mlcSongCode') or work_result.get('mlcsongCode')

                        if mlc_song_code:
                            work = mlc_client.get_work_by_id(mlc_song_code)
                            mlc_data = {
                                'recording': None,
                                'work': work,
                                'mlc_song_code': mlc_song_code
                            }
                            logger.info(f"Found '{song.title}' in MLC works database (song code: {mlc_song_code})")
                        elif work_result.get('writers') or work_result.get('publishers'):
                            # Work data returned directly from search
                            mlc_data = {
                                'recording': None,
                                'work': work_result,
                                'mlc_song_code': None
                            }
                            logger.info(f"Found '{song.title}' in MLC works search results")
                except Exception as e:
                    logger.warning(f"Works search failed for {song.title}: {e}")

            # Process MLC data if found
            if mlc_data and mlc_data.get('work'):
                work = mlc_data['work']
                track_result['mlc_registered'] = True
                track_result['mlc_song_code'] = mlc_data.get('mlc_song_code')
                track_result['iswc'] = work.get('iswc')
                total_registered += 1

                # Flag if song is registered but ISRC is not linked
                if not found_by_isrc:
                    track_result['isrc_missing'] = True
                    logger.warning(f"[MLC AUDIT] ISRC NOT LINKED: '{song.title}' (ISRC: {song.isrc}) found in MLC via fallback search but ISRC not in MLC database")

                # Extract writers and their IPI numbers
                writers = work.get('writers', [])
                for writer in writers:
                    writer_first = writer.get('writerFirstName', '') or ''
                    writer_last = writer.get('writerLastName', '') or ''
                    writer_full = f"{writer_first} {writer_last}".strip()
                    writer_ipi = writer.get('writerIPI') or writer.get('ipiNumber') or writer.get('ipi')

                    if writer_full:
                        track_result['writers'].append(writer_full)
                    if writer_ipi:
                        track_result['ipi_numbers'].append(writer_ipi)

                    # Check if user matches this writer
                    name_match = False
                    ipi_match = False

                    # Name matching (case insensitive, check both full name and parts)
                    writer_full_lower = writer_full.lower()
                    logger.info(f"Name check for '{song.title}': user_full='{user_full_name}', first='{user_first_name}', last='{user_last_name}', mlc_writer='{writer_full_lower}'")
                    if user_full_name and (
                        user_full_name in writer_full_lower or
                        writer_full_lower in user_full_name or
                        (user_first_name.lower() in writer_full_lower and
                         user_last_name.lower() in writer_full_lower)
                    ):
                        name_match = True
                        logger.info(f"  -> NAME MATCH for writer '{writer_full}'")

                    # IPI matching (with leading zero tolerance)
                    if user_ipi and writer_ipi:
                        logger.info(f"Writer IPI check for '{song.title}': user_ipi='{user_ipi}', mlc_writer_ipi='{writer_ipi}', writer_name='{writer_full}'")
                        if ipi_matches(user_ipi, writer_ipi):
                            ipi_match = True
                            logger.info(f"  -> IPI MATCH for writer '{writer_full}'")
                        else:
                            logger.info(f"  -> IPI NO MATCH")

                    if name_match or ipi_match:
                        track_result['user_is_writer'] = True
                        if name_match and ipi_match:
                            track_result['writer_matched_by'] = 'both'
                            track_result['user_matched_by'] = 'both'  # legacy
                        elif ipi_match:
                            track_result['writer_matched_by'] = 'ipi'
                            track_result['user_matched_by'] = 'ipi'  # legacy
                        elif name_match:
                            track_result['writer_matched_by'] = 'name'
                            track_result['user_matched_by'] = 'name'  # legacy

                # Extract publishers — flatten hierarchy to include parentPublishers
                raw_publishers = work.get('publishers', [])
                all_publishers = []
                def collect_publishers(pub_list):
                    for pub in pub_list:
                        all_publishers.append(pub)
                        parent_pubs = pub.get('parentPublishers', [])
                        if parent_pubs:
                            collect_publishers(parent_pubs)
                collect_publishers(raw_publishers)

                logger.info(f"[{song.title}] Found {len(raw_publishers)} top-level publishers, {len(all_publishers)} total (including nested)")
                for pub in all_publishers:
                    pub_name = pub.get('publisherName', '') or pub.get('name', '') or ''
                    # MLC API uses 'publisherIpiNumber' (camelCase)
                    pub_ipi = pub.get('publisherIpiNumber') or pub.get('publisherIPI') or pub.get('ipiNumber') or pub.get('ipi')
                    if pub_name:
                        track_result['publishers'].append(pub_name)

                        # Check if user matches publisher by name or IPI
                        pub_name_lower = pub_name.lower()
                        pub_name_match = False
                        pub_ipi_match = False

                        # Name matching: ONLY use publisher_name setting (not user's personal name)
                        # Publishers are companies/entities, not personal names
                        if user_publisher_name and user_publisher_name.lower() in pub_name_lower:
                            pub_name_match = True

                        # IPI matching for publishers (with leading zero tolerance)
                        logger.info(f"Publisher IPI check for '{song.title}': user_pub_ipi='{user_publisher_ipi}', mlc_pub_ipi='{pub_ipi}', pub_name='{pub_name}'")
                        if ipi_matches(user_publisher_ipi, pub_ipi):
                            pub_ipi_match = True
                            logger.info(f"  -> IPI MATCH for publisher '{pub_name}'")
                        elif user_publisher_ipi and pub_ipi:
                            logger.info(f"  -> IPI NO MATCH: '{user_publisher_ipi}' != '{pub_ipi}'")

                        if pub_name_match or pub_ipi_match:
                            track_result['user_is_publisher'] = True
                            # Set publisher_matched_by independently of writer matching
                            if pub_name_match and pub_ipi_match:
                                track_result['publisher_matched_by'] = 'both'
                            elif pub_ipi_match:
                                track_result['publisher_matched_by'] = 'ipi'
                            elif pub_name_match:
                                track_result['publisher_matched_by'] = 'name'

                # Update summary counts
                if track_result['user_is_writer'] or track_result['user_is_publisher']:
                    total_user_matches += 1
                if track_result['user_matched_by'] in ['ipi', 'both']:
                    total_ipi_matches += 1
            else:
                total_not_found += 1
                track_result['error'] = mlc_data.get('error') if mlc_data else 'Not found in MLC database'

        except Exception as e:
            logger.error(f"Error checking MLC for track {song.title}: {e}")
            track_result['error'] = str(e)
            total_not_found += 1

        tracks_result.append(track_result)

    # Check for unreported royalties - MLC registered but not in statements
    # Get all song titles from user's revenue transactions
    transaction_products = db.query(RevenueTransaction.product).filter(
        RevenueTransaction.user_id == user.id
    ).distinct().all()

    def normalize_title(title: str) -> str:
        """Normalize song title for matching - remove punctuation, accents, extra spaces."""
        if not title:
            return ""
        # Lowercase and strip
        normalized = title.lower().strip()
        # Convert accented characters to ASCII (ë -> e, é -> e, etc.)
        normalized = unicodedata.normalize('NFKD', normalized)
        normalized = ''.join(c for c in normalized if not unicodedata.combining(c))
        # Remove common punctuation and special characters
        normalized = re.sub(r'[^\w\s]', '', normalized)
        # Normalize whitespace
        normalized = ' '.join(normalized.split())
        return normalized

    transaction_titles_normalized = set()
    transaction_titles_raw = []
    for (product,) in transaction_products:
        if product:
            transaction_titles_raw.append(product.lower().strip())
            transaction_titles_normalized.add(normalize_title(product))

    # Find ALL catalog songs not in statements (not just MLC-registered)
    # This matches CatalogHealthSummary behavior for "Potentially Lost Revenue"
    unreported_songs = []  # List of (spotify_track_id, title, artist, publishing_royalty, is_mlc_registered)
    for track in tracks_result:
        title = track.get('title') or ''
        artist = track.get('artist') or ''
        spotify_track_id = track.get('spotify_track_id')
        publishing_royalty = track.get('publishing_royalty') or 0
        is_mlc_registered = track.get('mlc_registered', False)
        title_normalized = normalize_title(title)
        title_lower = title.lower().strip()

        # Check multiple matching strategies
        found_in_statements = False

        # Strategy 1: Exact normalized match
        if title_normalized in transaction_titles_normalized:
            found_in_statements = True

        # Strategy 2: Normalized title is substring of transaction (or vice versa)
        if not found_in_statements:
            for t_norm in transaction_titles_normalized:
                if title_normalized and t_norm:
                    if title_normalized in t_norm or t_norm in title_normalized:
                        found_in_statements = True
                        break

        # Strategy 3: Raw substring match (handles cases with punctuation)
        if not found_in_statements:
            for t_raw in transaction_titles_raw:
                if title_lower in t_raw or t_raw in title_lower:
                    found_in_statements = True
                    break

        if not found_in_statements:
            unreported_songs.append((spotify_track_id, title, artist, publishing_royalty, is_mlc_registered))
            mlc_status = "MLC registered" if is_mlc_registered else "not MLC registered"
            logger.info(f"Unreported song detected: {title} by {artist} ({mlc_status}, equity: {publishing_royalty})")

    # Trigger individual notification for each unreported song with potential revenue
    logger.info(f"Found {len(unreported_songs)} unreported songs for user {user.id}")
    if len(unreported_songs) > 0:
        try:
            notification_service = NotificationService(db)
            created_count = 0

            for spotify_track_id, song_title, song_artist, publishing_royalty, is_mlc_registered in unreported_songs:
                # Calculate estimated loss using same formula as CatalogHealthSummary
                # Uses PUBLISHING_ROYALTY_PER_STREAM ($0.001) and YOUTUBE_PUBLISHING_ROYALTY_PER_VIEW ($0.0004)
                estimated_loss = 0
                if spotify_track_id:
                    # Get the LATEST stats record (matching catalog.py streaming-revenue-analysis)
                    stats = db.query(StatsCache).filter(
                        StatsCache.spotify_track_id == spotify_track_id
                    ).order_by(StatsCache.date_added.desc()).first()
                    if stats:
                        # publishing_royalty is stored as decimal (0.5 = 50%)
                        # Use None check to properly handle 0% equity (0 should stay 0, not default to 1)
                        publishing_pct = publishing_royalty if publishing_royalty is not None else 1
                        spotify_streams = stats.spotify_playcount or 0
                        youtube_streams = stats.youtube_playcount or 0

                        # Same calculation as /catalog/streaming-revenue-analysis
                        estimated_loss = (
                            (spotify_streams * PUBLISHING_ROYALTY_PER_STREAM * publishing_pct) +
                            (youtube_streams * YOUTUBE_PUBLISHING_ROYALTY_PER_VIEW * publishing_pct)
                        )
                        equity_pct = (publishing_royalty if publishing_royalty is not None else 1) * 100
                        logger.info(f"Stats for {song_title}: spotify={spotify_streams}, youtube={youtube_streams}, equity={equity_pct:.0f}%, estimated_loss=${estimated_loss:.2f}")

                # Only create notification if there's potential revenue loss
                if estimated_loss > 0:
                    result = notification_service.create_unreported_revenue_notification(
                        user_id=user.id,
                        song_title=song_title,
                        artist=song_artist,
                        estimated_loss=estimated_loss,
                        is_mlc_registered=is_mlc_registered,
                    )
                    if result:
                        created_count += 1
                        logger.info(f"Created notification for {song_title}")
                    else:
                        logger.info(f"Notification deduplicated for {song_title}")
                else:
                    logger.info(f"Skipping notification for {song_title} - no estimated loss")
            logger.info(f"Created {created_count} unreported revenue notifications for user {user.id}")
        except Exception as e:
            logger.warning(f"Failed to create unreported revenue notifications: {e}")

    # Check for writer/IPI mismatch - songs found in MLC but user not matched
    # This is a separate scenario from "unreported" - the song IS in MLC but under different writers
    mismatched_songs = []  # List of (spotify_track_id, title, artist, publishing_royalty, writers)
    for track in tracks_result:
        # Song must be MLC registered but user is NOT matched as writer or publisher
        if track.get('mlc_registered', False) and not track.get('user_is_writer', False) and not track.get('user_is_publisher', False):
            spotify_track_id = track.get('spotify_track_id')
            title = track.get('title') or ''
            artist = track.get('artist') or ''
            publishing_royalty = track.get('publishing_royalty') or 0
            registered_writers = track.get('writers', [])

            mismatched_songs.append((spotify_track_id, title, artist, publishing_royalty, registered_writers))
            logger.info(f"Writer mismatch detected: {title} by {artist} - registered writers: {registered_writers}")

    # Create notifications for writer mismatch songs
    if len(mismatched_songs) > 0:
        try:
            notification_service = NotificationService(db)
            mismatch_count = 0

            for spotify_track_id, song_title, song_artist, publishing_royalty, registered_writers in mismatched_songs:
                # Calculate estimated loss using same formula
                estimated_loss = 0
                if spotify_track_id:
                    stats = db.query(StatsCache).filter(
                        StatsCache.spotify_track_id == spotify_track_id
                    ).order_by(StatsCache.date_added.desc()).first()
                    if stats:
                        # Use None check to properly handle 0% equity (0 should stay 0, not default to 1)
                        publishing_pct = publishing_royalty if publishing_royalty is not None else 1
                        spotify_streams = stats.spotify_playcount or 0
                        youtube_streams = stats.youtube_playcount or 0
                        estimated_loss = (
                            (spotify_streams * PUBLISHING_ROYALTY_PER_STREAM * publishing_pct) +
                            (youtube_streams * YOUTUBE_PUBLISHING_ROYALTY_PER_VIEW * publishing_pct)
                        )

                # Create notification for writer mismatch
                if estimated_loss > 0:
                    result = notification_service.create_writer_mismatch_notification(
                        user_id=user.id,
                        song_title=song_title,
                        artist=song_artist,
                        estimated_loss=estimated_loss,
                        registered_writers=registered_writers,
                    )
                    if result:
                        mismatch_count += 1
                        logger.info(f"Created writer mismatch notification for {song_title}")

            logger.info(f"Created {mismatch_count} writer mismatch notifications for user {user.id}")
        except Exception as e:
            logger.warning(f"Failed to create writer mismatch notifications: {e}")

    return {
        "tracks": tracks_result,
        "summary": {
            "total": len(catalog_entries),
            "mlc_registered": total_registered,
            "not_found": total_not_found,
            "user_matches": total_user_matches,
            "ipi_matches": total_ipi_matches,
            "isrc_missing": sum(1 for t in tracks_result if t.get('isrc_missing')),
            "unreported_in_statements": len(unreported_songs),
            "writer_mismatch": len(mismatched_songs)
        },
        "user_info": {
            "has_ipi": bool(user_writer_ipi or user_publisher_ipi),
            "has_legal_name": bool(user_full_name),
            "writer_ipi": user_writer_ipi if user_writer_ipi else None,
            "publisher_ipi": user_publisher_ipi if user_publisher_ipi else None,
            "publisher_name": user_publisher_name if user_publisher_name else None,
            "legal_name": f"{user_first_name} {user_last_name}".strip() if user_full_name else None
        }
    }


@mlc_audit_router.post("/enrich-works")
async def enrich_works_for_cwr(
    works: List[Dict[str, Any]],
    user: User = Depends(get_user),
    db: Session = Depends(get_session),
):
    """
    Enrich a list of works with MLC data for CWR file generation.

    For each work, searches MLC by ISRC (preferred) or title+writer to fill in:
    - ISWC (International Standard Work Code)
    - Writer IPI numbers (validated against MLC database)
    - Publisher names, IPIs, and roles from MLC registration
    - MLC Song Code for reference
    """
    try:
        settings = get_settings()
        mlc = MLCClient(
            username=settings.mlc_username,
            password=settings.mlc_password,
            api_url=settings.mlc_api_url,
        )
    except (ValueError, Exception) as e:
        raise HTTPException(status_code=503, detail=f"MLC API not available: {str(e)}")

    enriched = []

    for work in works:
        title = work.get("title", "").strip()
        work_id = work.get("id", "")
        isrc = ""
        recordings = work.get("recordings", [])
        if recordings and len(recordings) > 0:
            isrc = (recordings[0].get("isrc", "") or "").strip()
        writers = work.get("writers", [])

        result = {
            "workId": work_id,
            "title": title,
            "iswc": work.get("iswc", ""),
            "mlcSongCode": None,
            "enrichedWriters": [],
            "enrichedPublishers": [],
            "source": None,
            "error": None,
        }

        mlc_work = None

        # Strategy 1: ISRC lookup (most reliable)
        if isrc:
            try:
                mlc_data = mlc.get_complete_work_info_by_isrc(isrc)
                if mlc_data and mlc_data.get("work"):
                    mlc_work = mlc_data["work"]
                    result["mlcSongCode"] = mlc_data.get("mlc_song_code")
                    result["source"] = "isrc"
                    logger.info(f"MLC enrichment via ISRC for '{title}': found work")
            except Exception as e:
                logger.warning(f"MLC ISRC lookup failed for '{title}' ({isrc}): {e}")

        # Strategy 2: Title + writer name/IPI
        if not mlc_work and writers:
            for wr in writers[:2]:
                writer_name = f"{wr.get('firstName', '')} {wr.get('lastName', '')}".strip()
                writer_ipi = wr.get("ipi", "")
                if not writer_name and not writer_ipi:
                    continue
                try:
                    works_found = mlc.search_by_title_and_writer(
                        title=title,
                        writer_name=writer_name if writer_name else None,
                        writer_ipi=writer_ipi if writer_ipi else None,
                    )
                    if works_found and isinstance(works_found, list) and len(works_found) > 0:
                        title_lower = title.lower().strip()
                        for w in works_found[:5]:
                            w_title = (w.get("primaryTitle", "") or "").lower().strip()
                            if w_title == title_lower:
                                mlc_work = w
                                result["mlcSongCode"] = w.get("mlcSongCode")
                                result["source"] = "title_writer"
                                logger.info(f"MLC enrichment via title+writer for '{title}'")
                                break
                        if mlc_work:
                            break
                except Exception as e:
                    logger.warning(f"MLC title+writer search failed for '{title}': {e}")

        # Extract data from MLC work
        if mlc_work:
            mlc_iswc = mlc_work.get("iswc", "")
            if mlc_iswc and not result["iswc"]:
                result["iswc"] = mlc_iswc

            # Writers with IPIs and roles
            for mw in mlc_work.get("writers", []):
                w_first = mw.get("writerFirstName", "") or ""
                w_last = mw.get("writerLastName", "") or ""
                w_ipi = mw.get("writerIPI") or mw.get("ipiNumber") or mw.get("ipi") or ""
                w_role = mw.get("writerDesignationCode") or mw.get("role") or ""
                w_pro = mw.get("prAffiliationSociety") or mw.get("performingRightsSociety") or ""
                w_pr = mw.get("prOwnershipShare") or mw.get("performingRightsShare") or 0
                w_mr = mw.get("mrOwnershipShare") or mw.get("mechanicalRightsShare") or 0

                result["enrichedWriters"].append({
                    "firstName": w_first,
                    "lastName": w_last,
                    "ipi": str(w_ipi) if w_ipi else "",
                    "role": w_role,
                    "pro": w_pro,
                    "prShare": w_pr,
                    "mrShare": w_mr,
                })

            # Publishers — flatten hierarchy including parentPublishers
            raw_pubs = mlc_work.get("publishers", [])
            all_pubs = []

            def collect_pubs(pub_list):
                for pub in pub_list:
                    all_pubs.append(pub)
                    parents = pub.get("parentPublishers", [])
                    if parents:
                        collect_pubs(parents)

            collect_pubs(raw_pubs)

            for mp in all_pubs:
                p_name = mp.get("publisherName", "") or mp.get("name", "") or ""
                p_ipi = (
                    mp.get("publisherIpiNumber")
                    or mp.get("publisherIPI")
                    or mp.get("ipiNumber")
                    or mp.get("ipi")
                    or ""
                )
                p_role = mp.get("publisherType") or mp.get("role") or "E"
                p_pro = mp.get("prAffiliationSociety") or ""
                p_pr = mp.get("prCollectionShare") or mp.get("prOwnershipShare") or 0
                p_mr = mp.get("mrCollectionShare") or mp.get("mrOwnershipShare") or 0

                if p_name:
                    result["enrichedPublishers"].append({
                        "name": p_name,
                        "ipi": str(p_ipi) if p_ipi else "",
                        "role": p_role,
                        "pro": p_pro,
                        "prShare": p_pr,
                        "mrShare": p_mr,
                    })
        else:
            result["error"] = "Not found in MLC database"

        enriched.append(result)

    found_count = sum(1 for e in enriched if e["source"])
    logger.info(f"MLC enrichment complete: {found_count}/{len(works)} works found")

    return {
        "enriched": enriched,
        "summary": {
            "total": len(works),
            "found": found_count,
            "notFound": len(works) - found_count,
        },
    }
