from datetime import datetime
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import desc
from sqlalchemy.orm import Session

from app.database import get_session
from app.models.models import (
    Notification,
    NotificationCategory,
    NotificationPriority,
    NotificationStatus,
    User,
)
from app.routers.auth import get_user
from app.settings.settings import get_settings

notifications_router = APIRouter(
    prefix="/notifications",
    tags=["Notifications"],
)


# Pydantic models for API responses
class NotificationResponse(BaseModel):
    id: int
    category: str
    type: str
    title: str
    body: Optional[str] = None
    priority: Optional[str] = None
    status: str
    extra_data: Optional[dict] = None
    action_url: Optional[str] = None
    created_at: str
    read_at: Optional[str] = None

    class Config:
        from_attributes = True


class AttentionListResponse(BaseModel):
    notifications: List[NotificationResponse]
    total: int


class FeedListResponse(BaseModel):
    notifications: List[NotificationResponse]
    total: int
    has_more: bool


class UnreadCountResponse(BaseModel):
    attention_count: int
    feed_count: int
    total: int


class CreateNotificationRequest(BaseModel):
    category: str  # "attention" or "feed"
    type: str  # e.g., "revenue_discrepancy", "weekly_report"
    title: str
    body: Optional[str] = None
    priority: Optional[str] = None  # "high", "medium", "low" - only for attention
    extra_data: Optional[dict] = None
    action_url: Optional[str] = None


class BulkDismissRequest(BaseModel):
    notification_ids: List[int]


def notification_to_response(notification: Notification) -> NotificationResponse:
    """Convert a Notification model to a response object"""
    return NotificationResponse(
        id=notification.id,
        category=notification.category.value if notification.category else None,
        type=notification.type,
        title=notification.title,
        body=notification.body,
        priority=notification.priority.value if notification.priority else None,
        status=notification.status.value if notification.status else None,
        extra_data=notification.extra_data,
        action_url=notification.action_url,
        created_at=notification.created_at.isoformat() if notification.created_at else None,
        read_at=notification.read_at.isoformat() if notification.read_at else None,
    )


@notifications_router.get("/attention", response_model=AttentionListResponse)
def get_attention_notifications(
    user: User = Depends(get_user),
    session: Session = Depends(get_session),
):
    """Get all actionable notifications (needs attention)"""
    notifications = (
        session.query(Notification)
        .filter(
            Notification.user_id == user.id,
            Notification.category == NotificationCategory.ATTENTION,
            Notification.status.in_([NotificationStatus.UNREAD, NotificationStatus.READ]),
        )
        .order_by(
            # High priority first, then medium, then low
            desc(Notification.priority == NotificationPriority.HIGH),
            desc(Notification.priority == NotificationPriority.MEDIUM),
            desc(Notification.created_at),
        )
        .all()
    )

    return AttentionListResponse(
        notifications=[notification_to_response(n) for n in notifications],
        total=len(notifications),
    )


@notifications_router.get("/feed", response_model=FeedListResponse)
def get_feed_notifications(
    limit: int = 20,
    offset: int = 0,
    user: User = Depends(get_user),
    session: Session = Depends(get_session),
):
    """Get notification feed (informational notifications)"""
    query = (
        session.query(Notification)
        .filter(
            Notification.user_id == user.id,
            Notification.category == NotificationCategory.FEED,
            Notification.status.in_([NotificationStatus.UNREAD, NotificationStatus.READ]),
        )
        .order_by(desc(Notification.created_at))
    )

    total = query.count()
    notifications = query.offset(offset).limit(limit + 1).all()

    has_more = len(notifications) > limit
    if has_more:
        notifications = notifications[:limit]

    return FeedListResponse(
        notifications=[notification_to_response(n) for n in notifications],
        total=total,
        has_more=has_more,
    )


@notifications_router.get("/unread-count", response_model=UnreadCountResponse)
def get_unread_count(
    user: User = Depends(get_user),
    session: Session = Depends(get_session),
):
    """Get count of unread notifications by category"""
    attention_count = (
        session.query(Notification)
        .filter(
            Notification.user_id == user.id,
            Notification.category == NotificationCategory.ATTENTION,
            Notification.status == NotificationStatus.UNREAD,
        )
        .count()
    )

    feed_count = (
        session.query(Notification)
        .filter(
            Notification.user_id == user.id,
            Notification.category == NotificationCategory.FEED,
            Notification.status == NotificationStatus.UNREAD,
        )
        .count()
    )

    return UnreadCountResponse(
        attention_count=attention_count,
        feed_count=feed_count,
        total=attention_count + feed_count,
    )


@notifications_router.post("/{notification_id}/read")
def mark_as_read(
    notification_id: int,
    user: User = Depends(get_user),
    session: Session = Depends(get_session),
):
    """Mark a notification as read"""
    notification = (
        session.query(Notification)
        .filter(
            Notification.id == notification_id,
            Notification.user_id == user.id,
        )
        .first()
    )

    if not notification:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Notification not found",
        )

    notification.status = NotificationStatus.READ
    notification.read_at = datetime.now()
    session.commit()

    return {"success": True, "message": "Notification marked as read"}


@notifications_router.post("/{notification_id}/dismiss")
def dismiss_notification(
    notification_id: int,
    user: User = Depends(get_user),
    session: Session = Depends(get_session),
):
    """Dismiss/resolve a notification (removes from active list)"""
    notification = (
        session.query(Notification)
        .filter(
            Notification.id == notification_id,
            Notification.user_id == user.id,
        )
        .first()
    )

    if not notification:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Notification not found",
        )

    notification.status = NotificationStatus.DISMISSED
    session.commit()

    return {"success": True, "message": "Notification dismissed"}


@notifications_router.post("/dismiss-bulk")
def dismiss_notifications_bulk(
    request: BulkDismissRequest,
    user: User = Depends(get_user),
    session: Session = Depends(get_session),
):
    """Dismiss multiple notifications at once"""
    if not request.notification_ids:
        return {"success": True, "message": "No notifications to dismiss", "count": 0}

    count = (
        session.query(Notification)
        .filter(
            Notification.user_id == user.id,
            Notification.id.in_(request.notification_ids),
        )
        .update(
            {Notification.status: NotificationStatus.DISMISSED},
            synchronize_session=False,
        )
    )
    session.commit()

    return {"success": True, "message": f"Dismissed {count} notifications", "count": count}


@notifications_router.post("/{notification_id}/resolve")
def resolve_notification(
    notification_id: int,
    user: User = Depends(get_user),
    session: Session = Depends(get_session),
):
    """Mark an attention item as resolved"""
    notification = (
        session.query(Notification)
        .filter(
            Notification.id == notification_id,
            Notification.user_id == user.id,
            Notification.category == NotificationCategory.ATTENTION,
        )
        .first()
    )

    if not notification:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Attention item not found",
        )

    notification.status = NotificationStatus.RESOLVED
    session.commit()

    return {"success": True, "message": "Item resolved"}


@notifications_router.post("/mark-all-read")
def mark_all_read(
    category: Optional[str] = None,
    user: User = Depends(get_user),
    session: Session = Depends(get_session),
):
    """Mark all notifications as read, optionally filtered by category"""
    query = session.query(Notification).filter(
        Notification.user_id == user.id,
        Notification.status == NotificationStatus.UNREAD,
    )

    if category:
        if category == "attention":
            query = query.filter(Notification.category == NotificationCategory.ATTENTION)
        elif category == "feed":
            query = query.filter(Notification.category == NotificationCategory.FEED)

    now = datetime.now()
    count = query.update(
        {Notification.status: NotificationStatus.READ, Notification.read_at: now},
        synchronize_session=False,
    )
    session.commit()

    return {"success": True, "message": f"Marked {count} notifications as read"}


@notifications_router.post("/create")
def create_notification(
    request: CreateNotificationRequest,
    user: User = Depends(get_user),
    session: Session = Depends(get_session),
):
    """Create a new notification (for testing/admin purposes)"""
    # Parse category
    try:
        category = NotificationCategory(request.category)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid category. Must be one of: {[c.value for c in NotificationCategory]}",
        )

    # Parse priority if provided
    priority = None
    if request.priority:
        try:
            priority = NotificationPriority(request.priority)
        except ValueError:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Invalid priority. Must be one of: {[p.value for p in NotificationPriority]}",
            )

    notification = Notification(
        user_id=user.id,
        category=category,
        type=request.type,
        title=request.title,
        body=request.body,
        priority=priority,
        status=NotificationStatus.UNREAD,
        extra_data=request.extra_data,
        action_url=request.action_url,
    )

    session.add(notification)
    session.commit()
    session.refresh(notification)

    return {
        "success": True,
        "notification": notification_to_response(notification),
    }


# Development-only endpoints for testing scheduled tasks
@notifications_router.post("/trigger-daily")
def trigger_daily_notifications(
    user: User = Depends(get_user),
    session: Session = Depends(get_session),
):
    """Manually trigger daily notification checks (development only)"""
    settings = get_settings()
    if settings.mode != "development":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="This endpoint is only available in development mode",
        )

    from app.services.notification_service import run_daily_notifications

    try:
        run_daily_notifications(session)
        return {"success": True, "message": "Daily notifications triggered"}
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error running daily notifications: {str(e)}",
        )


@notifications_router.post("/trigger-weekly")
def trigger_weekly_notifications(
    user: User = Depends(get_user),
    session: Session = Depends(get_session),
):
    """Manually trigger weekly notification checks (development only)"""
    settings = get_settings()
    if settings.mode != "development":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="This endpoint is only available in development mode",
        )

    from app.services.notification_service import run_weekly_notifications

    try:
        run_weekly_notifications(session)
        return {"success": True, "message": "Weekly notifications triggered"}
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error running weekly notifications: {str(e)}",
        )


@notifications_router.post("/generate-revenue-report")
def generate_revenue_report_notification(
    user: User = Depends(get_user),
    session: Session = Depends(get_session),
):
    """Generate a weekly revenue report notification using actual catalog data (development only)"""
    settings = get_settings()
    if settings.mode != "development":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="This endpoint is only available in development mode",
        )

    from datetime import timedelta
    from app.models.models import UserCatalog, StatsCache
    from app.services.notification_service import NotificationService
    from app.crud.stats import PUBLISHING_ROYALTY_PER_STREAM, YOUTUBE_PUBLISHING_ROYALTY_PER_VIEW

    try:
        # Get user's catalog
        catalog_entries = session.query(UserCatalog).filter(
            UserCatalog.user_id == user.id
        ).all()

        if not catalog_entries:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="No catalog entries found for user",
            )

        # Calculate weekly stream delta and estimated revenue
        seven_days_ago = datetime.now() - timedelta(days=7)
        weekly_streams = 0
        weekly_revenue = 0
        tracks_with_activity = 0

        for entry in catalog_entries:
            song = entry.song
            if song and song.spotify_track_id:
                # Get equity for this track (match catalog.py behavior: None = 0)
                equity = entry.publishing_royalty or 0

                # Get latest stats
                latest_stats = session.query(StatsCache).filter(
                    StatsCache.spotify_track_id == song.spotify_track_id
                ).order_by(StatsCache.date_added.desc()).first()

                # Get stats from ~7 days ago
                old_stats = session.query(StatsCache).filter(
                    StatsCache.spotify_track_id == song.spotify_track_id,
                    StatsCache.date_added <= seven_days_ago
                ).order_by(StatsCache.date_added.desc()).first()

                if latest_stats:
                    # Calculate stream deltas
                    current_spotify = latest_stats.spotify_playcount or 0
                    current_youtube = latest_stats.youtube_playcount or 0

                    if old_stats:
                        old_spotify = old_stats.spotify_playcount or 0
                        old_youtube = old_stats.youtube_playcount or 0
                        spotify_delta = max(0, current_spotify - old_spotify)
                        youtube_delta = max(0, current_youtube - old_youtube)
                    else:
                        # Estimate weekly as 1/52 of total
                        spotify_delta = current_spotify // 52
                        youtube_delta = current_youtube // 52

                    stream_delta = spotify_delta + youtube_delta
                    if stream_delta > 0:
                        weekly_streams += stream_delta
                        tracks_with_activity += 1

                        # Calculate estimated revenue based on streams and equity
                        track_revenue = (
                            (spotify_delta * PUBLISHING_ROYALTY_PER_STREAM * equity) +
                            (youtube_delta * YOUTUBE_PUBLISHING_ROYALTY_PER_VIEW * equity)
                        )
                        weekly_revenue += track_revenue

        # Create the notification
        notification_service = NotificationService(session)
        week_label = seven_days_ago.strftime("%b %d")

        notification = notification_service.create_weekly_report_notification(
            user_id=user.id,
            week_label=week_label,
            total_streams=int(weekly_streams),
            total_revenue=float(weekly_revenue),
        )

        return {
            "success": True,
            "message": "Revenue report notification generated",
            "data": {
                "catalog_tracks": len(catalog_entries),
                "tracks_with_activity": tracks_with_activity,
                "weekly_streams": weekly_streams,
                "weekly_revenue": round(weekly_revenue, 2),
                "week_label": week_label,
            },
            "notification": notification_to_response(notification) if notification else None,
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error generating revenue report: {str(e)}",
        )


@notifications_router.post("/test-song")
def send_test_song_notification(
    user: User = Depends(get_user),
    session: Session = Depends(get_session),
):
    """Send a test notification about a random song from user's catalog"""
    from app.models.models import UserCatalog
    from app.services.notification_service import NotificationService
    import random

    # Get a random song from user's catalog
    catalog_entries = (
        session.query(UserCatalog)
        .filter(UserCatalog.user_id == user.id)
        .all()
    )

    if not catalog_entries:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No songs in catalog to test with",
        )

    # Pick a random song
    entry = random.choice(catalog_entries)
    song = entry.song

    # Create a test notification about this song
    notification_service = NotificationService(session)

    # Create a milestone notification (more interesting than generic)
    notification = notification_service.create_milestone_notification(
        user_id=user.id,
        song_title=song.title,
        song_id=song.id,
        milestone=1_000_000,
        platform="Spotify",
    )

    return {
        "success": True,
        "message": f"Test notification sent for song: {song.title}",
        "notification": notification_to_response(notification) if notification else None,
    }
