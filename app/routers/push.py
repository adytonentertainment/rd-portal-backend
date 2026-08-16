"""
Push Notification API Router
Handles Web Push subscription and mobile device token management
"""
from datetime import datetime
from typing import Optional, List

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.database import get_session
from app.models.models import PushSubscription, DeviceToken, User
from app.routers.auth import get_user
from app.settings.settings import get_settings

push_router = APIRouter(
    prefix="/push",
    tags=["Push Notifications"],
)

# Also create a router for /notifications/device-token (iOS app compatibility)
notifications_device_router = APIRouter(
    prefix="/notifications",
    tags=["Push Notifications"],
)


class PushSubscriptionKeys(BaseModel):
    p256dh: str
    auth: str


class PushSubscriptionRequest(BaseModel):
    endpoint: str
    keys: PushSubscriptionKeys
    user_agent: Optional[str] = None


class PushSubscriptionResponse(BaseModel):
    id: int
    endpoint: str
    created_at: str

    class Config:
        from_attributes = True


class VapidPublicKeyResponse(BaseModel):
    public_key: str


@push_router.get("/vapid-public-key", response_model=VapidPublicKeyResponse)
def get_vapid_public_key():
    """Get the VAPID public key for push subscription"""
    settings = get_settings()
    if not settings.vapid_public_key:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Push notifications are not configured"
        )
    return VapidPublicKeyResponse(public_key=settings.vapid_public_key)


@push_router.post("/subscribe", response_model=PushSubscriptionResponse)
def subscribe_to_push(
    subscription: PushSubscriptionRequest,
    user: User = Depends(get_user),
    session: Session = Depends(get_session),
):
    """Subscribe to push notifications"""
    # Check if subscription already exists
    existing = session.query(PushSubscription).filter(
        PushSubscription.endpoint == subscription.endpoint
    ).first()

    if existing:
        # Update existing subscription (might be different user or renewed)
        existing.user_id = user.id
        existing.p256dh_key = subscription.keys.p256dh
        existing.auth_key = subscription.keys.auth
        existing.user_agent = subscription.user_agent
        session.commit()
        return PushSubscriptionResponse(
            id=existing.id,
            endpoint=existing.endpoint,
            created_at=existing.created_at.isoformat()
        )

    # Create new subscription
    push_sub = PushSubscription(
        user_id=user.id,
        endpoint=subscription.endpoint,
        p256dh_key=subscription.keys.p256dh,
        auth_key=subscription.keys.auth,
        user_agent=subscription.user_agent,
    )
    session.add(push_sub)
    session.commit()

    return PushSubscriptionResponse(
        id=push_sub.id,
        endpoint=push_sub.endpoint,
        created_at=push_sub.created_at.isoformat()
    )


@push_router.delete("/unsubscribe")
def unsubscribe_from_push(
    endpoint: str,
    user: User = Depends(get_user),
    session: Session = Depends(get_session),
):
    """Unsubscribe from push notifications"""
    subscription = session.query(PushSubscription).filter(
        PushSubscription.endpoint == endpoint,
        PushSubscription.user_id == user.id
    ).first()

    if not subscription:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Subscription not found"
        )

    session.delete(subscription)
    session.commit()

    return {"message": "Unsubscribed successfully"}


@push_router.get("/status")
def get_push_status(
    user: User = Depends(get_user),
    session: Session = Depends(get_session),
):
    """Get push notification status for current user"""
    subscriptions = session.query(PushSubscription).filter(
        PushSubscription.user_id == user.id
    ).all()

    device_tokens = session.query(DeviceToken).filter(
        DeviceToken.user_id == user.id
    ).all()

    return {
        "enabled": len(subscriptions) > 0 or len(device_tokens) > 0,
        "web_subscription_count": len(subscriptions),
        "device_token_count": len(device_tokens),
        "subscriptions": [
            {
                "id": sub.id,
                "user_agent": sub.user_agent,
                "created_at": sub.created_at.isoformat(),
                "last_used_at": sub.last_used_at.isoformat() if sub.last_used_at else None
            }
            for sub in subscriptions
        ],
        "device_tokens": [
            {
                "id": dt.id,
                "platform": dt.platform,
                "bundle_id": dt.bundle_id,
                "created_at": dt.created_at.isoformat(),
                "last_used_at": dt.last_used_at.isoformat() if dt.last_used_at else None
            }
            for dt in device_tokens
        ]
    }


# ==================== MOBILE DEVICE TOKEN ENDPOINTS ====================

class DeviceTokenRequest(BaseModel):
    token: str
    platform: str = "ios"  # "ios" or "android"
    bundle_id: Optional[str] = None
    device_name: Optional[str] = None


class DeviceTokenResponse(BaseModel):
    id: int
    token: str
    platform: str
    created_at: str

    class Config:
        from_attributes = True


class DeviceTokenDeleteRequest(BaseModel):
    token: str


@push_router.post("/device-token", response_model=DeviceTokenResponse)
@notifications_device_router.post("/device-token", response_model=DeviceTokenResponse)
def register_device_token(
    request: DeviceTokenRequest,
    user: User = Depends(get_user),
    session: Session = Depends(get_session),
):
    """Register a mobile device token for push notifications (iOS/Android)"""
    # Check if token already exists
    existing = session.query(DeviceToken).filter(
        DeviceToken.token == request.token
    ).first()

    if existing:
        # Update existing token (might be different user after logout/login)
        existing.user_id = user.id
        existing.platform = request.platform
        existing.bundle_id = request.bundle_id
        existing.device_name = request.device_name
        session.commit()
        return DeviceTokenResponse(
            id=existing.id,
            token=existing.token,
            platform=existing.platform,
            created_at=existing.created_at.isoformat()
        )

    # Create new device token
    device_token = DeviceToken(
        user_id=user.id,
        token=request.token,
        platform=request.platform,
        bundle_id=request.bundle_id,
        device_name=request.device_name,
    )
    session.add(device_token)
    session.commit()

    return DeviceTokenResponse(
        id=device_token.id,
        token=device_token.token,
        platform=device_token.platform,
        created_at=device_token.created_at.isoformat()
    )


@push_router.delete("/device-token")
@notifications_device_router.delete("/device-token")
def unregister_device_token(
    request: DeviceTokenDeleteRequest,
    user: User = Depends(get_user),
    session: Session = Depends(get_session),
):
    """Unregister a mobile device token (on logout)"""
    device_token = session.query(DeviceToken).filter(
        DeviceToken.token == request.token
    ).first()

    if not device_token:
        # Token not found is OK - might have already been deleted
        return {"message": "Token not found or already removed"}

    session.delete(device_token)
    session.commit()

    return {"message": "Device token unregistered successfully"}


@push_router.post("/test")
def send_test_push(
    user: User = Depends(get_user),
    session: Session = Depends(get_session),
):
    """Send a test push notification to all user devices (web + iOS)"""
    from app.services.push_service import PushService
    from app.services.apns_service import APNsService

    results = {
        "web_push": {"sent": 0, "failed": 0},
        "apns": {"sent": 0, "failed": 0},
    }

    # Send web push
    push_service = PushService(session)
    web_result = push_service.send_to_user(
        user_id=user.id,
        title="Test Notification",
        body="This is a test push notification from Verax!",
        data={"type": "test"}
    )
    results["web_push"] = web_result

    # Send APNs push
    apns_service = APNsService(session)
    apns_result = apns_service.send_push_to_user(
        user_id=user.id,
        title="Test Notification",
        body="This is a test push notification from Verax!",
        data={"type": "test"}
    )
    results["apns"] = apns_result

    return {
        "message": "Test notifications sent",
        "results": results
    }
