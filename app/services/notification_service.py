"""
Notification Service
Handles creating notifications for various events in the Verax platform.
"""
import logging
from datetime import datetime, timedelta
from typing import Dict, Any, Optional, List
from sqlalchemy.orm import Session
from sqlalchemy import func

from app.models.models import (
    Notification,
    NotificationCategory,
    NotificationPriority,
    NotificationStatus,
    User,
    UserCatalog,
    Songs,
    StatsCache,
    RevenueTransaction,
    Agreement,
)

logger = logging.getLogger(__name__)


class NotificationService:
    """Service for creating and managing notifications."""

    def __init__(self, db: Session, send_push: bool = True):
        self.db = db
        self.send_push = send_push
        self._push_service = None
        self._apns_service = None

    @property
    def push_service(self):
        """Lazy-load web push service to avoid circular imports"""
        if self._push_service is None and self.send_push:
            from app.services.push_service import PushService
            self._push_service = PushService(self.db)
        return self._push_service

    @property
    def apns_service(self):
        """Lazy-load APNs service to avoid circular imports"""
        if self._apns_service is None and self.send_push:
            from app.services.apns_service import APNsService
            self._apns_service = APNsService(self.db)
        return self._apns_service

    def _send_push_notification(self, notification: Notification) -> None:
        """Send push notification to all platforms (web + iOS)"""
        if not self.send_push:
            return

        # Send to web browsers
        if self.push_service:
            try:
                result = self.push_service.send_notification_as_push(notification)
                if result.get("success", 0) > 0:
                    logger.info(f"Web push sent for notification {notification.id}: {result}")
                elif result.get("error"):
                    logger.debug(f"Web push skipped for notification {notification.id}: {result.get('error')}")
            except Exception as e:
                logger.error(f"Failed to send web push for notification {notification.id}: {e}")

        # Send to iOS devices
        if self.apns_service:
            try:
                result = self.apns_service.send_notification_as_push(notification)
                if result.get("success", 0) > 0:
                    logger.info(f"APNs push sent for notification {notification.id}: {result}")
                elif result.get("error"):
                    logger.debug(f"APNs push skipped for notification {notification.id}: {result.get('error')}")
            except Exception as e:
                logger.error(f"Failed to send APNs push for notification {notification.id}: {e}")

    # ==================== REVENUE NOTIFICATIONS ====================

    def create_statement_processed_notification(
        self,
        user_id: int,
        statement_id: int,
        filename: str,
        transaction_count: int,
        total_amount: float,
        songs_synced: int = 0,
    ) -> Notification:
        """Create notification when a revenue statement is processed."""
        body = f"Your {filename} statement has been imported. {transaction_count} transactions added"
        if songs_synced > 0:
            body += f", {songs_synced} songs synced to catalog"
        body += f". Total: ${total_amount:,.2f}"

        notification = Notification(
            user_id=user_id,
            category=NotificationCategory.FEED,
            type="statement_processed",
            title="Revenue statement processed",
            body=body,
            status=NotificationStatus.UNREAD,
            extra_data={
                "statement_id": statement_id,
                "filename": filename,
                "transactions": transaction_count,
                "amount": total_amount,
                "songs_synced": songs_synced,
            },
            action_url="/revenue",
        )
        self.db.add(notification)
        self.db.commit()
        self._send_push_notification(notification)
        return notification

    def create_revenue_discrepancy_notification(
        self,
        user_id: int,
        song_title: str,
        song_id: int,
        expected: float,
        received: float,
        platform: str = "Unknown",
    ) -> Notification:
        """Create notification when a revenue discrepancy is detected."""
        difference = expected - received
        notification = Notification(
            user_id=user_id,
            category=NotificationCategory.ATTENTION,
            type="revenue_discrepancy",
            title="Revenue discrepancy found",
            body=f'"{song_title}" expected ${expected:,.2f}, received ${received:,.2f}. Potential underpayment of ${difference:,.2f}.',
            priority=NotificationPriority.HIGH,
            status=NotificationStatus.UNREAD,
            extra_data={
                "song_id": song_id,
                "expected": expected,
                "received": received,
                "amount": difference,
                "platform": platform,
            },
            action_url="/revenue",
        )
        self.db.add(notification)
        self.db.commit()
        self._send_push_notification(notification)
        return notification

    def create_song_missing_from_statement_notification(
        self,
        user_id: int,
        song_title: str,
        song_id: int,
        total_streams: int,
        distributor: str,
    ) -> Notification:
        """Create notification when a song with streams is missing from statement."""
        streams_formatted = f"{total_streams / 1_000_000:.1f}M" if total_streams >= 1_000_000 else f"{total_streams:,}"
        notification = Notification(
            user_id=user_id,
            category=NotificationCategory.ATTENTION,
            type="song_missing",
            title="Song missing in statement",
            body=f'"{song_title}" has {streams_formatted} streams but was not reported in your {distributor} statement.',
            priority=NotificationPriority.HIGH,
            status=NotificationStatus.UNREAD,
            extra_data={
                "song_id": song_id,
                "streams": total_streams,
                "distributor": distributor,
            },
            action_url="/revenue",
        )
        self.db.add(notification)
        self.db.commit()
        self._send_push_notification(notification)
        return notification

    def create_revenue_spike_notification(
        self,
        user_id: int,
        song_title: str,
        song_id: int,
        amount: float,
        percentage_increase: int,
        period: str = "this week",
    ) -> Notification:
        """Create notification when a song has a significant revenue spike."""
        notification = Notification(
            user_id=user_id,
            category=NotificationCategory.FEED,
            type="revenue_spike",
            title="Revenue spike detected",
            body=f'"{song_title}" earned ${amount:,.2f} {period} - {percentage_increase}% above your average.',
            status=NotificationStatus.UNREAD,
            extra_data={
                "song_id": song_id,
                "amount": amount,
                "percentage_increase": percentage_increase,
            },
            action_url="/revenue",
        )
        self.db.add(notification)
        self.db.commit()
        self._send_push_notification(notification)
        return notification

    def create_weekly_report_notification(
        self,
        user_id: int,
        week_label: str,
        total_streams: int,
        total_revenue: float,
    ) -> Notification:
        """Create weekly earnings summary notification."""
        streams_formatted = f"{total_streams / 1_000_000:.1f}M" if total_streams >= 1_000_000 else f"{total_streams:,}"
        notification = Notification(
            user_id=user_id,
            category=NotificationCategory.FEED,
            type="weekly_report",
            title="Weekly report ready",
            body=f"Week of {week_label}: {streams_formatted} streams, ${total_revenue:,.2f} revenue across all platforms.",
            status=NotificationStatus.UNREAD,
            extra_data={
                "streams": total_streams,
                "revenue": total_revenue,
                "week": week_label,
            },
            action_url="/revenue",
        )
        self.db.add(notification)
        self.db.commit()
        self._send_push_notification(notification)
        return notification

    def create_approaching_recoupment_notification(
        self,
        user_id: int,
        song_title: str,
        song_id: int,
        percentage: int,
        remaining: float,
    ) -> Notification:
        """Create notification when a song is close to recouping its advance."""
        notification = Notification(
            user_id=user_id,
            category=NotificationCategory.FEED,
            type="approaching_recoupment",
            title="Almost recouped",
            body=f'"{song_title}" is {percentage}% recouped. Only ${remaining:,.2f} remaining.',
            status=NotificationStatus.UNREAD,
            extra_data={
                "song_id": song_id,
                "percentage": percentage,
                "remaining": remaining,
            },
            action_url="/revenue",
        )
        self.db.add(notification)
        self.db.commit()
        self._send_push_notification(notification)
        return notification

    # ==================== TUNESCAN NOTIFICATIONS ====================

    def create_new_match_notification(
        self,
        user_id: int,
        song_title: str,
        song_id: int,
        platform: str,
        licensed: bool,
        match_confidence: float = 0,
    ) -> Notification:
        """Create notification when TuneScan finds a new match."""
        license_status = "licensed use confirmed" if licensed else "license status unknown"
        notification = Notification(
            user_id=user_id,
            category=NotificationCategory.FEED,
            type="new_match",
            title="New match detected",
            body=f'"{song_title}" found on {platform} - {license_status}.',
            status=NotificationStatus.UNREAD,
            extra_data={
                "song_id": song_id,
                "platform": platform,
                "licensed": licensed,
                "confidence": match_confidence,
            },
            action_url="/tunescan",
        )
        self.db.add(notification)
        self.db.commit()
        self._send_push_notification(notification)
        return notification

    def create_unauthorized_use_notification(
        self,
        user_id: int,
        song_title: str,
        song_id: int,
        platform: str,
        views: int = 0,
        url: str = None,
    ) -> Notification:
        """Create notification when potential unauthorized use is detected."""
        views_formatted = f"{views / 1_000_000:.1f}M" if views >= 1_000_000 else f"{views:,}"
        notification = Notification(
            user_id=user_id,
            category=NotificationCategory.ATTENTION,
            type="unauthorized_use",
            title="Potential unauthorized use",
            body=f'TuneScan detected "{song_title}" in a {platform} video with {views_formatted} views. No license found.',
            priority=NotificationPriority.HIGH,
            status=NotificationStatus.UNREAD,
            extra_data={
                "song_id": song_id,
                "platform": platform,
                "views": views,
                "url": url,
            },
            action_url="/tunescan",
        )
        self.db.add(notification)
        self.db.commit()
        self._send_push_notification(notification)
        return notification

    def create_scan_complete_notification(
        self,
        user_id: int,
        file_id: str,
        filename: str,
        match_found: bool,
        match_title: str = None,
        match_artist: str = None,
        confidence: float = 0,
    ) -> Notification:
        """Create notification when a TuneScan scan completes."""
        if match_found and match_title:
            body = f'Scan complete for "{filename}". Match found: "{match_title}" by {match_artist} ({confidence:.0f}% confidence).'
        else:
            body = f'Scan complete for "{filename}". No matches found.'

        notification = Notification(
            user_id=user_id,
            category=NotificationCategory.FEED,
            type="scan_complete",
            title="TuneScan complete",
            body=body,
            status=NotificationStatus.UNREAD,
            extra_data={
                "file_id": file_id,
                "filename": filename,
                "match_found": match_found,
                "match_title": match_title,
                "match_artist": match_artist,
                "confidence": confidence,
            },
            action_url="/tunescan",
        )
        self.db.add(notification)
        self.db.commit()
        self._send_push_notification(notification)
        return notification

    def create_auto_scan_complete_notification(
        self,
        user_id: int,
        matches_found: int,
    ) -> Notification:
        """Create notification when daily auto-scan completes."""
        notification = Notification(
            user_id=user_id,
            category=NotificationCategory.FEED,
            type="new_match_auto",
            title="Auto-scan complete",
            body=f"Daily scan found {matches_found} new uses of your catalog across streaming platforms.",
            status=NotificationStatus.UNREAD,
            extra_data={"matches_found": matches_found},
            action_url="/tunescan",
        )
        self.db.add(notification)
        self.db.commit()
        self._send_push_notification(notification)
        return notification

    def create_auto_scan_match_notification(
        self,
        user_id: int,
        match_title: str,
        match_artist: str,
        platform: str = "Spotify",
        confidence: float = 0,
    ) -> Notification:
        """
        Create notification when auto-rescan finds a new match.
        Format: "New match found (auto-scan)" - "New match detected" - "{Song}" found on Spotify
        """
        notification = Notification(
            user_id=user_id,
            category=NotificationCategory.FEED,
            type="new_match_auto",
            title="New match found (auto-scan)",
            body=f'"New match detected" - "{match_title}" by {match_artist} found on {platform}.',
            status=NotificationStatus.UNREAD,
            extra_data={
                "match_title": match_title,
                "match_artist": match_artist,
                "platform": platform,
                "confidence": confidence,
            },
            action_url="/tunescan",
        )
        self.db.add(notification)
        self.db.commit()
        self._send_push_notification(notification)
        return notification

    def create_upload_match_notification(
        self,
        user_id: int,
        match_title: str,
        match_artist: str,
        platform: str = "Spotify",
        confidence: float = 0,
    ) -> Notification:
        """
        Create notification when initial upload finds a match.
        Format: "TuneScan upload complete" - "New match detected" - "{Song}" by Artist found on Spotify.
        """
        notification = Notification(
            user_id=user_id,
            category=NotificationCategory.FEED,
            type="upload_match",
            title="TuneScan upload complete",
            body=f'"New match detected" - "{match_title}" by {match_artist} found on {platform}.',
            status=NotificationStatus.UNREAD,
            extra_data={
                "match_title": match_title,
                "match_artist": match_artist,
                "platform": platform,
                "confidence": confidence,
            },
            action_url="/tunescan",
        )
        self.db.add(notification)
        self.db.commit()
        self._send_push_notification(notification)
        return notification

    def create_rescan_triggered_notification(
        self,
        user_id: int,
        file_id: str,
        filename: str,
        is_auto: bool = False,
    ) -> Notification:
        """Create notification when a rescan is triggered (manual or auto)."""
        if is_auto:
            title = "Auto-rescan triggered"
            body = f'Auto-rescanning "{filename}" for new matches. Results will appear shortly.'
        else:
            title = "Rescan started"
            body = f'Rescanning "{filename}" for new matches. Results will appear shortly.'

        notification = Notification(
            user_id=user_id,
            category=NotificationCategory.FEED,
            type="catalog_update",
            title=title,
            body=body,
            status=NotificationStatus.UNREAD,
            extra_data={
                "file_id": file_id,
                "filename": filename,
                "is_auto": is_auto,
            },
            action_url="/tunescan",
        )
        self.db.add(notification)
        self.db.commit()
        return notification

    # ==================== CATALOG NOTIFICATIONS ====================

    def create_milestone_notification(
        self,
        user_id: int,
        song_title: str,
        song_id: int,
        milestone: int,
        platform: str = "Spotify",
    ) -> Notification:
        """Create notification when a song hits a streaming milestone."""
        milestone_formatted = f"{milestone / 1_000_000:.0f} million" if milestone >= 1_000_000 else f"{milestone:,}"
        notification = Notification(
            user_id=user_id,
            category=NotificationCategory.FEED,
            type="milestone",
            title="Milestone reached!",
            body=f'"{song_title}" just hit {milestone_formatted} streams on {platform}!',
            status=NotificationStatus.UNREAD,
            extra_data={
                "song_id": song_id,
                "milestone": milestone,
                "platform": platform,
            },
            action_url="/catalog",
        )
        self.db.add(notification)
        self.db.commit()
        return notification

    def create_trending_notification(
        self,
        user_id: int,
        song_title: str,
        song_id: int,
        percentage_increase: int,
        period: str = "48h",
    ) -> Notification:
        """Create notification when a song is trending."""
        notification = Notification(
            user_id=user_id,
            category=NotificationCategory.FEED,
            type="trending",
            title="Trending now",
            body=f'"{song_title}" streams up {percentage_increase}% in the last {period}.',
            status=NotificationStatus.UNREAD,
            extra_data={
                "song_id": song_id,
                "percentage_increase": percentage_increase,
                "period": period,
            },
            action_url="/catalog",
        )
        self.db.add(notification)
        self.db.commit()
        return notification

    def create_catalog_synced_notification(
        self,
        user_id: int,
        tracks_added: int,
        platform: str,
    ) -> Notification:
        """Create notification when catalog is synced with a platform."""
        notification = Notification(
            user_id=user_id,
            category=NotificationCategory.FEED,
            type="catalog_update",
            title="Catalog synced",
            body=f"Your catalog has been synced with {platform}. {tracks_added} new tracks detected.",
            status=NotificationStatus.UNREAD,
            extra_data={
                "tracks_added": tracks_added,
                "platform": platform,
            },
            action_url="/catalog",
        )
        self.db.add(notification)
        self.db.commit()
        return notification

    def create_revenue_leak_notification(
        self,
        user_id: int,
        song_title: str,
        song_id: int,
        amount: float,
        issue: str,
        pro: str = None,
    ) -> Notification:
        """Create notification when a revenue leak is detected."""
        notification = Notification(
            user_id=user_id,
            category=NotificationCategory.ATTENTION,
            type="revenue_leak",
            title="Revenue leak found",
            body=f'"{song_title}" has {issue}. Estimated loss: ${amount:,.2f}/month.',
            priority=NotificationPriority.HIGH,
            status=NotificationStatus.UNREAD,
            extra_data={
                "song_id": song_id,
                "amount": amount,
                "pro": pro,
            },
            action_url="/catalog",
        )
        self.db.add(notification)
        self.db.commit()
        return notification

    def create_mlc_unreported_royalty_notification(
        self,
        user_id: int,
        song_title: str,
        estimated_loss: float = 0,
    ) -> Notification:
        """Create notification for a single MLC-registered song missing from statements."""
        # Deduplicate - only one notification per song per day
        if self.deduplicate_notification(
            user_id, "mlc_unreported", "song_title", song_title, hours=24
        ):
            return None

        body = f'"{song_title}" is registered with the MLC but not appearing in your statements.'
        if estimated_loss > 0:
            body += f" Estimated loss: ${estimated_loss:,.2f}"

        notification = Notification(
            user_id=user_id,
            category=NotificationCategory.ATTENTION,
            type="revenue_leak",
            title="Unreported royalties detected",
            body=body,
            priority=NotificationPriority.HIGH,
            status=NotificationStatus.UNREAD,
            extra_data={
                "song_title": song_title,
                "amount": estimated_loss,
            },
            action_url="/revenue",
        )
        self.db.add(notification)
        self.db.commit()
        return notification

    def create_unreported_revenue_notification(
        self,
        user_id: int,
        song_title: str,
        artist: str = "",
        estimated_loss: float = 0,
        is_mlc_registered: bool = False,
    ) -> Notification:
        """Create notification for a song missing from statements with potential revenue loss."""
        # Deduplicate - check for existing notification (including dismissed) for this song
        if self.deduplicate_notification(
            user_id, "revenue_leak", "song_title", song_title, hours=168  # 7 days
        ):
            return None

        # Format song display with artist if available
        song_display = f'"{song_title}"'
        if artist:
            song_display += f" by {artist}"

        # Different messaging based on MLC registration status
        if is_mlc_registered:
            body = f'{song_display} is registered with the MLC but not appearing in your statements.'
        else:
            body = f'{song_display} is not appearing in your statements and not found in PRO/CMO database.'

        notification = Notification(
            user_id=user_id,
            category=NotificationCategory.ATTENTION,
            type="revenue_leak",
            title="Potential revenue leak detected",
            body=body,
            priority=NotificationPriority.HIGH,
            status=NotificationStatus.UNREAD,
            extra_data={
                "song_title": song_title,
                "artist": artist,
                "amount": round(estimated_loss, 2),
                "is_mlc_registered": is_mlc_registered,
            },
            action_url="/revenue",
        )
        self.db.add(notification)
        self.db.commit()
        return notification

    def create_writer_mismatch_notification(
        self,
        user_id: int,
        song_title: str,
        artist: str = "",
        estimated_loss: float = 0,
        registered_writers: List[str] = None,
    ) -> Notification:
        """Create notification when song is found in MLC but user's name/IPI doesn't match registered writers."""
        # Deduplicate - check for existing notification (including dismissed) for this song
        if self.deduplicate_notification(
            user_id, "revenue_leak", "song_title", song_title, hours=168  # 7 days
        ):
            return None

        # Format song display with artist if available
        song_display = f'"{song_title}"'
        if artist:
            song_display += f" by {artist}"

        # Show who IS registered if available
        if registered_writers and len(registered_writers) > 0:
            writers_str = ", ".join(registered_writers[:3])  # Show first 3
            if len(registered_writers) > 3:
                writers_str += f" (+{len(registered_writers) - 3} more)"
            body = f'{song_display} is registered with the MLC under different writers: {writers_str}. Your name/IPI does not match.'
        else:
            body = f'{song_display} is registered with the MLC but your name/IPI does not match the registered writers.'

        notification = Notification(
            user_id=user_id,
            category=NotificationCategory.ATTENTION,
            type="revenue_leak",
            title="Potential revenue leak detected",
            body=body,
            priority=NotificationPriority.HIGH,
            status=NotificationStatus.UNREAD,
            extra_data={
                "song_title": song_title,
                "artist": artist,
                "amount": round(estimated_loss, 2),
                "is_mlc_registered": True,
                "writer_mismatch": True,
                "registered_writers": registered_writers or [],
            },
            action_url="/revenue",
        )
        self.db.add(notification)
        self.db.commit()
        return notification

    # ==================== AGREEMENT NOTIFICATIONS ====================

    def create_agreement_parsed_notification(
        self,
        user_id: int,
        agreement_id: int,
        filename: str,
        agreement_type: str,
        red_flags: int = 0,
    ) -> Notification:
        """Create notification when an agreement is parsed."""
        if red_flags > 0:
            body = f"Your {filename} ({agreement_type}) has been analyzed. {red_flags} red flag(s) detected - review recommended."
            priority = NotificationPriority.HIGH
            category = NotificationCategory.ATTENTION
        else:
            body = f"Your {filename} ({agreement_type}) has been analyzed. No major red flags detected."
            priority = None
            category = NotificationCategory.FEED

        notification = Notification(
            user_id=user_id,
            category=category,
            type="agreement_parsed",
            title="Agreement analyzed",
            body=body,
            priority=priority,
            status=NotificationStatus.UNREAD,
            extra_data={
                "agreement_id": agreement_id,
                "filename": filename,
                "agreement_type": agreement_type,
                "red_flags": red_flags,
            },
            action_url="/agreements",
        )
        self.db.add(notification)
        self.db.commit()
        return notification

    def create_critical_red_flag_notification(
        self,
        user_id: int,
        agreement_id: int,
        filename: str,
        red_flag: dict,
    ) -> Notification:
        """Create notification for a critical red flag detected in an agreement.

        Args:
            user_id: The user ID
            agreement_id: The agreement ID
            filename: The agreement filename
            red_flag: Dict containing id, name, severity, impact, recommendation, clause
        """
        flag_id = red_flag.get("id", "")
        flag_name = red_flag.get("name", "Unknown Issue")
        flag_impact = red_flag.get("impact", "This clause may significantly affect your rights.")

        notification = Notification(
            user_id=user_id,
            category=NotificationCategory.ATTENTION,
            type="critical_red_flag",
            title=f"Critical Issue in {filename}",
            body=f"{flag_name} ({flag_id}) detected: {flag_impact}",
            priority=NotificationPriority.HIGH,
            status=NotificationStatus.UNREAD,
            extra_data={
                "agreement_id": agreement_id,
                "filename": filename,
                "red_flag_id": flag_id,
                "red_flag_name": flag_name,
                "severity": red_flag.get("severity", "CRITICAL"),
                "clause": red_flag.get("clause", ""),
                "impact": flag_impact,
                "recommendation": red_flag.get("recommendation", ""),
            },
            action_url=f"/agreements/{agreement_id}",
        )
        self.db.add(notification)
        self.db.commit()
        return notification

    def create_termination_expiring_notification(
        self,
        user_id: int,
        agreement_id: int,
        publisher: str,
        days_remaining: int,
    ) -> Notification:
        """Create notification when termination window is closing."""
        notification = Notification(
            user_id=user_id,
            category=NotificationCategory.ATTENTION,
            type="termination_expiring",
            title="Termination window closing",
            body=f"You have {days_remaining} days to exercise your termination rights for the {publisher} deal.",
            priority=NotificationPriority.HIGH,
            status=NotificationStatus.UNREAD,
            extra_data={
                "agreement_id": agreement_id,
                "days_remaining": days_remaining,
                "publisher": publisher,
            },
            action_url="/agreements",
        )
        self.db.add(notification)
        self.db.commit()
        return notification

    def create_audit_expiring_notification(
        self,
        user_id: int,
        agreement_id: int,
        publisher: str,
        days_remaining: int,
    ) -> Notification:
        """Create notification when audit window is closing."""
        notification = Notification(
            user_id=user_id,
            category=NotificationCategory.ATTENTION,
            type="audit_expiring",
            title="Audit window closing",
            body=f"Your right to audit {publisher} expires in {days_remaining} days.",
            priority=NotificationPriority.MEDIUM if days_remaining > 14 else NotificationPriority.HIGH,
            status=NotificationStatus.UNREAD,
            extra_data={
                "agreement_id": agreement_id,
                "days_remaining": days_remaining,
                "publisher": publisher,
            },
            action_url="/agreements",
        )
        self.db.add(notification)
        self.db.commit()
        return notification

    def create_collection_ending_notification(
        self,
        user_id: int,
        agreement_id: int,
        song_title: str,
        song_id: int,
        publisher: str,
        end_date: str,
    ) -> Notification:
        """Create notification when post-term collection is ending."""
        notification = Notification(
            user_id=user_id,
            category=NotificationCategory.ATTENTION,
            type="collection_ending",
            title="Post-term collection ending",
            body=f'{publisher} stops collecting for "{song_title}" on {end_date}.',
            priority=NotificationPriority.MEDIUM,
            status=NotificationStatus.UNREAD,
            extra_data={
                "agreement_id": agreement_id,
                "song_id": song_id,
                "publisher": publisher,
                "end_date": end_date,
            },
            action_url="/agreements",
        )
        self.db.add(notification)
        self.db.commit()
        return notification

    def create_renewal_reminder_notification(
        self,
        user_id: int,
        agreement_id: int,
        publisher: str,
        renewal_date: str,
        days_remaining: int,
    ) -> Notification:
        """Create notification when agreement is about to auto-renew."""
        notification = Notification(
            user_id=user_id,
            category=NotificationCategory.ATTENTION,
            type="renewal_reminder",
            title="Agreement auto-renewal reminder",
            body=f"Your agreement with {publisher} auto-renews on {renewal_date}. {days_remaining} days to cancel.",
            priority=NotificationPriority.MEDIUM,
            status=NotificationStatus.UNREAD,
            extra_data={
                "agreement_id": agreement_id,
                "publisher": publisher,
                "renewal_date": renewal_date,
                "days_remaining": days_remaining,
            },
            action_url="/agreements",
        )
        self.db.add(notification)
        self.db.commit()
        return notification

    # ==================== HELPER METHODS ====================

    def check_and_notify_milestone(
        self,
        user_id: int,
        song_id: int,
        current_streams: int,
        previous_streams: int,
    ) -> Optional[Notification]:
        """Check if a milestone was crossed and create notification if so."""
        milestones = [
            1_000_000,     # 1M
            5_000_000,     # 5M
            10_000_000,    # 10M
            25_000_000,    # 25M
            50_000_000,    # 50M
            100_000_000,   # 100M
            250_000_000,   # 250M
            500_000_000,   # 500M
            1_000_000_000, # 1B
        ]

        for milestone in milestones:
            if previous_streams < milestone <= current_streams:
                # Get song title
                song = self.db.query(Songs).filter(Songs.id == song_id).first()
                if song:
                    return self.create_milestone_notification(
                        user_id=user_id,
                        song_title=song.title,
                        song_id=song_id,
                        milestone=milestone,
                    )
        return None

    def check_and_notify_trending(
        self,
        user_id: int,
        song_id: int,
        current_streams: int,
        previous_streams: int,
        threshold_percent: int = 100,
    ) -> Optional[Notification]:
        """Check if a song is trending and create notification if so."""
        if previous_streams <= 0:
            return None

        percentage_increase = ((current_streams - previous_streams) / previous_streams) * 100

        if percentage_increase >= threshold_percent:
            song = self.db.query(Songs).filter(Songs.id == song_id).first()
            if song:
                return self.create_trending_notification(
                    user_id=user_id,
                    song_title=song.title,
                    song_id=song_id,
                    percentage_increase=int(percentage_increase),
                )
        return None

    def deduplicate_notification(
        self,
        user_id: int,
        notification_type: str,
        extra_data_key: str,
        extra_data_value: Any,
        hours: int = 24,
    ) -> bool:
        """Check if a similar notification exists. Returns True if duplicate exists.

        Checks for:
        1. Active notifications (UNREAD/READ) - regardless of age
        2. Dismissed notifications within the hours window - respects user dismissal for a while
        3. Recent notifications within hours window
        """
        cutoff = datetime.now() - timedelta(hours=hours)

        # Check for any active (non-dismissed) notification for this item
        active = self.db.query(Notification).filter(
            Notification.user_id == user_id,
            Notification.type == notification_type,
            Notification.status.in_([NotificationStatus.UNREAD, NotificationStatus.READ]),
        ).all()

        for notif in active:
            if notif.extra_data and notif.extra_data.get(extra_data_key) == extra_data_value:
                return True

        # Check for recently dismissed notifications (respect user's dismissal)
        dismissed = self.db.query(Notification).filter(
            Notification.user_id == user_id,
            Notification.type == notification_type,
            Notification.status == NotificationStatus.DISMISSED,
            Notification.created_at >= cutoff,
        ).all()

        for notif in dismissed:
            if notif.extra_data and notif.extra_data.get(extra_data_key) == extra_data_value:
                return True

        return False


# ==================== SCHEDULED TASK FUNCTIONS ====================

def run_daily_notifications(db: Session):
    """
    Run daily notification checks for all users.
    Called by scheduled task (e.g., Celery, APScheduler).
    """
    from app.models.models import User

    users = db.query(User).all()
    service = NotificationService(db)

    for user in users:
        try:
            # Check agreement deadlines
            _check_agreement_deadlines(service, user.id, db)

            # Check for missing songs in statements
            _check_missing_songs(service, user.id, db)

            # Check for revenue discrepancies
            _check_revenue_discrepancies(service, user.id, db)

            # Check for milestones and trending
            _check_catalog_milestones(service, user.id, db)

        except Exception as e:
            print(f"[ERROR] Daily notifications for user {user.id}: {e}")
            continue


def run_weekly_notifications(db: Session):
    """
    Run weekly notification checks for all users.
    Called by scheduled task (e.g., every Monday).
    """
    from app.models.models import User

    users = db.query(User).all()
    service = NotificationService(db)

    for user in users:
        try:
            # Generate weekly report
            _generate_weekly_report(service, user.id, db)
        except Exception as e:
            print(f"[ERROR] Weekly notifications for user {user.id}: {e}")
            continue


def _check_agreement_deadlines(service: NotificationService, user_id: int, db: Session):
    """Check for upcoming agreement deadlines."""
    agreements = db.query(Agreement).filter(Agreement.user_id == user_id).all()

    for agreement in agreements:
        if not agreement.parsed_content:
            continue

        parsed = agreement.parsed_content
        renewal = parsed.get("renewal", {})

        # Check renewal date
        if renewal and renewal.get("auto_renews") == "Yes":
            next_renewal = renewal.get("next_renewal_date")
            notice_days = int(renewal.get("termination_notice_days") or 30)

            if next_renewal:
                try:
                    renewal_date = datetime.strptime(next_renewal, "%Y-%m-%d")
                    days_until = (renewal_date - datetime.now()).days

                    # Notify if within notice period + 7 days buffer
                    if 0 < days_until <= notice_days + 7:
                        if not service.deduplicate_notification(
                            user_id, "renewal_reminder", "agreement_id", agreement.id
                        ):
                            assignee = parsed.get("assignee", "Publisher")
                            service.create_renewal_reminder_notification(
                                user_id=user_id,
                                agreement_id=agreement.id,
                                publisher=assignee,
                                renewal_date=next_renewal,
                                days_remaining=days_until,
                            )
                except ValueError:
                    pass

        # Check audit window
        audit_rights = parsed.get("audit_rights", {})
        audit_window = audit_rights.get("audit_window")
        # This would require tracking statement dates - placeholder for now


def _check_missing_songs(service: NotificationService, user_id: int, db: Session):
    """Check for songs in catalog missing from revenue statements."""
    # Get user's catalog songs
    catalog = db.query(UserCatalog).filter(UserCatalog.user_id == user_id).all()

    for entry in catalog:
        song = entry.song

        # Check if song has significant streams
        stats = db.query(StatsCache).filter(
            StatsCache.spotify_track_id == song.spotify_track_id
        ).order_by(StatsCache.date_added.desc()).first()

        if not stats:
            continue

        total_streams = (stats.spotify_playcount or 0) + (stats.youtube_playcount or 0)

        # Only alert if song has 100k+ streams
        if total_streams < 100_000:
            continue

        # Check if any revenue transactions exist for this song
        transactions = db.query(RevenueTransaction).filter(
            RevenueTransaction.user_id == user_id,
            RevenueTransaction.product.ilike(f"%{song.title}%")
        ).first()

        if not transactions:
            # No revenue found for this song
            if not service.deduplicate_notification(
                user_id, "song_missing", "song_id", song.id, hours=168  # Weekly
            ):
                service.create_song_missing_from_statement_notification(
                    user_id=user_id,
                    song_title=song.title,
                    song_id=song.id,
                    total_streams=total_streams,
                    distributor="your distributor",
                )


def _check_revenue_discrepancies(service: NotificationService, user_id: int, db: Session):
    """Check for revenue discrepancies between expected and actual."""
    PUBLISHING_RATE_PER_STREAM = 0.0009

    catalog = db.query(UserCatalog).filter(UserCatalog.user_id == user_id).all()

    for entry in catalog:
        song = entry.song
        publishing_split = entry.publishing_royalty or 0

        if publishing_split <= 0:
            continue

        # Get latest stats
        stats = db.query(StatsCache).filter(
            StatsCache.spotify_track_id == song.spotify_track_id
        ).order_by(StatsCache.date_added.desc()).first()

        if not stats:
            continue

        total_streams = (stats.spotify_playcount or 0) + (stats.youtube_playcount or 0)
        expected_revenue = total_streams * PUBLISHING_RATE_PER_STREAM * publishing_split

        # Get actual revenue
        actual_revenue = db.query(func.sum(RevenueTransaction.amount)).filter(
            RevenueTransaction.user_id == user_id,
            RevenueTransaction.product.ilike(f"%{song.title}%")
        ).scalar() or 0

        # Check for significant discrepancy (>30% underpayment and >$100)
        if expected_revenue > 100 and actual_revenue < expected_revenue * 0.7:
            if not service.deduplicate_notification(
                user_id, "revenue_discrepancy", "song_id", song.id, hours=168  # Weekly
            ):
                service.create_revenue_discrepancy_notification(
                    user_id=user_id,
                    song_title=song.title,
                    song_id=song.id,
                    expected=expected_revenue,
                    received=actual_revenue,
                )


def _check_catalog_milestones(service: NotificationService, user_id: int, db: Session):
    """Check for streaming milestones and trending songs."""
    catalog = db.query(UserCatalog).filter(UserCatalog.user_id == user_id).all()

    for entry in catalog:
        song = entry.song

        # Get current and previous stats
        stats = db.query(StatsCache).filter(
            StatsCache.spotify_track_id == song.spotify_track_id
        ).order_by(StatsCache.date_added.desc()).limit(2).all()

        if len(stats) < 2:
            continue

        current_streams = (stats[0].spotify_playcount or 0) + (stats[0].youtube_playcount or 0)
        previous_streams = (stats[1].spotify_playcount or 0) + (stats[1].youtube_playcount or 0)

        # Check milestones
        service.check_and_notify_milestone(
            user_id=user_id,
            song_id=song.id,
            current_streams=current_streams,
            previous_streams=previous_streams,
        )

        # Check trending
        service.check_and_notify_trending(
            user_id=user_id,
            song_id=song.id,
            current_streams=current_streams,
            previous_streams=previous_streams,
            threshold_percent=200,  # 200% increase = trending
        )


def _generate_weekly_report(service: NotificationService, user_id: int, db: Session):
    """Generate weekly summary notification."""
    # Get total revenue for the past week
    week_ago = datetime.now() - timedelta(days=7)

    total_revenue = db.query(func.sum(RevenueTransaction.amount)).filter(
        RevenueTransaction.user_id == user_id,
        RevenueTransaction.date >= week_ago.isoformat()
    ).scalar() or 0

    # Get total streams (approximate from latest stats)
    catalog = db.query(UserCatalog).filter(UserCatalog.user_id == user_id).all()
    total_streams = 0

    for entry in catalog:
        stats = db.query(StatsCache).filter(
            StatsCache.spotify_track_id == entry.song.spotify_track_id
        ).order_by(StatsCache.date_added.desc()).first()

        if stats:
            total_streams += (stats.spotify_playcount or 0) + (stats.youtube_playcount or 0)

    # Only create report if there's activity
    if total_revenue > 0 or total_streams > 0:
        week_label = (datetime.now() - timedelta(days=7)).strftime("%b %d")
        service.create_weekly_report_notification(
            user_id=user_id,
            week_label=week_label,
            total_streams=total_streams,
            total_revenue=total_revenue,
        )
