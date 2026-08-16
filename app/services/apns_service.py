"""
APNs (Apple Push Notification Service) Service
Handles sending push notifications to iOS devices
"""
import asyncio
import logging
from datetime import datetime
from typing import Optional, Dict, Any, List

from sqlalchemy.orm import Session

from app.models.models import DeviceToken, Notification
from app.settings.settings import get_settings

logger = logging.getLogger(__name__)

# Try to import aioapns, but don't fail if not installed
try:
    from aioapns import APNs, NotificationRequest, PushType
    APNS_AVAILABLE = True
except ImportError:
    APNS_AVAILABLE = False
    logger.warning("aioapns not installed - APNs push notifications disabled")


class APNsService:
    """Service for sending Apple Push Notifications"""

    def __init__(self, db: Session):
        self.db = db
        self.settings = get_settings()
        self._client = None

    def _is_configured(self) -> bool:
        """Check if APNs is properly configured"""
        if not APNS_AVAILABLE:
            return False
        return all([
            self.settings.apns_key_id,
            self.settings.apns_team_id,
            self.settings.apns_key_path,
            self.settings.apns_bundle_id,
        ])

    def _get_client(self):
        """Get or create APNs client"""
        if not self._is_configured():
            return None

        if self._client is None:
            try:
                # Read key content from file (aioapns expects key content, not path)
                with open(self.settings.apns_key_path, 'r') as f:
                    key_content = f.read()

                self._client = APNs(
                    key=key_content,
                    key_id=self.settings.apns_key_id,
                    team_id=self.settings.apns_team_id,
                    topic=self.settings.apns_bundle_id,
                    use_sandbox=self.settings.apns_use_sandbox,
                )
            except Exception as e:
                logger.error(f"Failed to create APNs client: {e}")
                return None

        return self._client

    async def send_push_to_token(
        self,
        device_token: str,
        title: str,
        body: str,
        badge: Optional[int] = None,
        sound: str = "default",
        category: Optional[str] = None,
        thread_id: Optional[str] = None,
        data: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        Send push notification to a single iOS device token.

        Returns dict with success status and any errors.
        """
        client = self._get_client()
        if not client:
            return {"success": False, "error": "APNs not configured"}

        # Build notification payload
        alert = {"title": title, "body": body}

        aps = {
            "alert": alert,
            "sound": sound,
        }

        if badge is not None:
            aps["badge"] = badge

        if category:
            aps["category"] = category

        if thread_id:
            aps["thread-id"] = thread_id

        # Build full payload
        payload = {"aps": aps}

        # Add custom data
        if data:
            payload.update(data)

        try:
            request = NotificationRequest(
                device_token=device_token,
                message=payload,
                push_type=PushType.ALERT,
            )

            response = await client.send_notification(request)

            if response.is_successful:
                return {"success": True}
            else:
                error_reason = response.description or "Unknown error"
                logger.error(f"APNs push failed: {error_reason}")

                # Handle specific error cases
                if response.description in ["BadDeviceToken", "Unregistered"]:
                    # Token is invalid, should be removed
                    return {
                        "success": False,
                        "error": error_reason,
                        "remove_token": True
                    }

                return {"success": False, "error": error_reason}

        except Exception as e:
            logger.error(f"APNs push exception: {e}")
            return {"success": False, "error": str(e)}

    def send_push_to_token_sync(
        self,
        device_token: str,
        title: str,
        body: str,
        **kwargs
    ) -> Dict[str, Any]:
        """Synchronous wrapper for send_push_to_token"""
        try:
            loop = asyncio.get_event_loop()
        except RuntimeError:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)

        return loop.run_until_complete(
            self.send_push_to_token(device_token, title, body, **kwargs)
        )

    def send_push_to_user(
        self,
        user_id: int,
        title: str,
        body: str,
        badge: Optional[int] = None,
        sound: str = "default",
        category: Optional[str] = None,
        data: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        Send push notification to all iOS devices for a user.

        Returns dict with success/failure counts.
        """
        if not self._is_configured():
            return {"success": 0, "failed": 0, "error": "APNs not configured"}

        # Get all iOS device tokens for this user
        tokens = self.db.query(DeviceToken).filter(
            DeviceToken.user_id == user_id,
            DeviceToken.platform == "ios"
        ).all()

        if not tokens:
            return {"success": 0, "failed": 0, "error": "No iOS devices"}

        success_count = 0
        failed_count = 0
        tokens_to_remove = []

        for token in tokens:
            result = self.send_push_to_token_sync(
                device_token=token.token,
                title=title,
                body=body,
                badge=badge,
                sound=sound,
                category=category,
                data=data,
            )

            if result.get("success"):
                success_count += 1
                token.last_used_at = datetime.now()
            else:
                failed_count += 1
                if result.get("remove_token"):
                    tokens_to_remove.append(token)

        # Remove invalid tokens
        for token in tokens_to_remove:
            logger.info(f"Removing invalid APNs token {token.id}")
            self.db.delete(token)

        self.db.commit()

        return {
            "success": success_count,
            "failed": failed_count,
            "removed": len(tokens_to_remove)
        }

    def send_notification_as_push(self, notification: Notification) -> Dict[str, Any]:
        """
        Send a Notification model instance as an APNs push.
        Convenience method that extracts fields from the notification.
        """
        # Determine category for notification actions
        category = None
        if notification.category:
            category = f"verax_{notification.category.value}"

        return self.send_push_to_user(
            user_id=notification.user_id,
            title=notification.title,
            body=notification.body or "",
            category=category,
            data={
                "notification_id": notification.id,
                "type": notification.type,
                "action_url": notification.action_url,
                "priority": notification.priority.value if notification.priority else None,
            }
        )
