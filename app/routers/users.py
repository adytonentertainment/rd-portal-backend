from typing import Dict, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.database import get_session
from app.models.models import User
from app.routers.auth import get_user

users_router = APIRouter(
    prefix="/users",
    tags=["Users"],
)


# Default notification settings - all enabled
DEFAULT_NOTIFICATION_SETTINGS: Dict[str, Dict[str, bool]] = {
    "revenue_discrepancy": {"in_app": True, "email": True, "push": True},
    "revenue_spike": {"in_app": True, "email": True, "push": True},
    "weekly_report": {"in_app": True, "email": True, "push": True},
    "new_match": {"in_app": True, "email": True, "push": True},
    "unauthorized_use": {"in_app": True, "email": True, "push": True},
    "scan_complete": {"in_app": True, "email": True, "push": True},
    "milestone": {"in_app": True, "email": True, "push": True},
    "trending": {"in_app": True, "email": True, "push": True},
    "agreement_parsed": {"in_app": True, "email": True, "push": True},
    "critical_red_flag": {"in_app": True, "email": True, "push": True},
    "termination_expiring": {"in_app": True, "email": True, "push": True},
    "statement_processed": {"in_app": True, "email": True, "push": True},
}


class NotificationSettingsResponse(BaseModel):
    settings: Dict[str, Dict[str, bool]]


class NotificationSettingsRequest(BaseModel):
    settings: Dict[str, Dict[str, bool]]


@users_router.get("/notification-settings", response_model=NotificationSettingsResponse)
def get_notification_settings(
    user: User = Depends(get_user),
    session: Session = Depends(get_session),
):
    """Get user's notification settings"""
    # Return stored settings or defaults if none set
    if user.notification_settings:
        # Merge with defaults to ensure all notification types are present
        merged_settings = DEFAULT_NOTIFICATION_SETTINGS.copy()
        for notification_type, channels in user.notification_settings.items():
            if notification_type in merged_settings:
                merged_settings[notification_type].update(channels)
            else:
                merged_settings[notification_type] = channels
        return NotificationSettingsResponse(settings=merged_settings)

    return NotificationSettingsResponse(settings=DEFAULT_NOTIFICATION_SETTINGS)


@users_router.put("/notification-settings", response_model=NotificationSettingsResponse)
def update_notification_settings(
    request: NotificationSettingsRequest,
    user: User = Depends(get_user),
    session: Session = Depends(get_session),
):
    """Update user's notification settings"""
    # Validate that all notification types and channels are valid
    valid_types = set(DEFAULT_NOTIFICATION_SETTINGS.keys())
    valid_channels = {"in_app", "email", "push"}

    for notification_type, channels in request.settings.items():
        if notification_type not in valid_types:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Invalid notification type: {notification_type}",
            )
        for channel in channels.keys():
            if channel not in valid_channels:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Invalid channel: {channel}",
                )

    # Update user's notification settings
    user.notification_settings = request.settings
    session.commit()

    return NotificationSettingsResponse(settings=request.settings)
