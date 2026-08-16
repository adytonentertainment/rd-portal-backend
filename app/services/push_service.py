"""
Web Push Service
Handles sending push notifications to subscribed browsers/devices
"""
import json
import logging
from datetime import datetime
from typing import Optional, List, Dict, Any

from pywebpush import webpush, WebPushException
from sqlalchemy.orm import Session

from app.models.models import PushSubscription, Notification
from app.settings.settings import get_settings

logger = logging.getLogger(__name__)


class PushService:
    """Service for sending Web Push notifications"""

    def __init__(self, db: Session):
        self.db = db
        self.settings = get_settings()

    def _get_vapid_claims(self) -> Dict[str, str]:
        """Get VAPID claims for authentication"""
        return {
            "sub": f"mailto:{self.settings.vapid_contact_email or self.settings.contact_email}"
        }

    def send_push_to_user(
        self,
        user_id: int,
        title: str,
        body: str,
        icon: Optional[str] = "/logo192.png",
        badge: Optional[str] = "/badge.png",
        url: Optional[str] = None,
        tag: Optional[str] = None,
        data: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        Send push notification to all subscriptions for a user.

        Returns dict with success/failure counts and any errors.
        """
        if not self.settings.vapid_private_key or not self.settings.vapid_public_key:
            logger.warning("Push notifications not configured - missing VAPID keys")
            return {"success": 0, "failed": 0, "error": "Push not configured"}

        subscriptions = self.db.query(PushSubscription).filter(
            PushSubscription.user_id == user_id
        ).all()

        if not subscriptions:
            return {"success": 0, "failed": 0, "error": "No subscriptions"}

        payload = {
            "title": title,
            "body": body,
            "icon": icon,
            "badge": badge,
            "tag": tag or f"verax-{datetime.now().timestamp()}",
            "data": {
                "url": url or "/notifications",
                **(data or {})
            },
            "requireInteraction": False,
            "renotify": False,
        }

        success_count = 0
        failed_count = 0
        errors = []

        for subscription in subscriptions:
            try:
                webpush(
                    subscription_info={
                        "endpoint": subscription.endpoint,
                        "keys": {
                            "p256dh": subscription.p256dh_key,
                            "auth": subscription.auth_key,
                        }
                    },
                    data=json.dumps(payload),
                    vapid_private_key=self.settings.vapid_private_key,
                    vapid_claims=self._get_vapid_claims(),
                )

                # Update last_used_at
                subscription.last_used_at = datetime.now()
                success_count += 1

            except WebPushException as e:
                failed_count += 1
                errors.append(str(e))

                # If subscription expired or unsubscribed (410 Gone), remove it
                if e.response and e.response.status_code == 410:
                    logger.info(f"Removing expired subscription {subscription.id}")
                    self.db.delete(subscription)
                elif e.response and e.response.status_code == 404:
                    logger.info(f"Removing invalid subscription {subscription.id}")
                    self.db.delete(subscription)
                else:
                    logger.error(f"Push failed for subscription {subscription.id}: {e}")

            except Exception as e:
                failed_count += 1
                errors.append(str(e))
                logger.error(f"Unexpected error sending push: {e}")

        self.db.commit()

        return {
            "success": success_count,
            "failed": failed_count,
            "errors": errors if errors else None
        }

    def send_notification_as_push(self, notification: Notification) -> Dict[str, Any]:
        """
        Send a Notification model instance as a push notification.
        Convenience method that extracts fields from the notification.
        """
        return self.send_push_to_user(
            user_id=notification.user_id,
            title=notification.title,
            body=notification.body or "",
            url=notification.action_url,
            tag=f"verax-{notification.type}-{notification.id}",
            data={
                "notification_id": notification.id,
                "type": notification.type,
                "category": notification.category.value if notification.category else None,
                "priority": notification.priority.value if notification.priority else None,
            }
        )

    def send_push_to_all_users(
        self,
        title: str,
        body: str,
        user_ids: Optional[List[int]] = None,
        **kwargs
    ) -> Dict[str, Any]:
        """
        Send push notification to multiple users.
        If user_ids is None, sends to ALL subscribed users.
        """
        query = self.db.query(PushSubscription.user_id).distinct()

        if user_ids:
            query = query.filter(PushSubscription.user_id.in_(user_ids))

        target_user_ids = [row[0] for row in query.all()]

        total_success = 0
        total_failed = 0

        for user_id in target_user_ids:
            result = self.send_push_to_user(user_id, title, body, **kwargs)
            total_success += result.get("success", 0)
            total_failed += result.get("failed", 0)

        return {
            "users_targeted": len(target_user_ids),
            "success": total_success,
            "failed": total_failed
        }
