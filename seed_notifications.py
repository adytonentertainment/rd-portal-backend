"""Seed script to generate dummy notifications for testing"""
import sys
sys.path.insert(0, '.')

from datetime import datetime, timedelta
from app.database import SessionLocal
from app.models.models import (
    Notification,
    NotificationCategory,
    NotificationPriority,
    NotificationStatus,
    User,
)

def seed_notifications():
    session = SessionLocal()

    # Get the first user
    user = session.query(User).first()
    if not user:
        print("No users found in database. Please create a user first.")
        return

    print(f"Creating notifications for user: {user.email}")

    # Clear existing notifications for this user
    session.query(Notification).filter(Notification.user_id == user.id).delete()

    # Attention items (actionable) - matching the spec
    attention_notifications = [
        {
            "category": NotificationCategory.ATTENTION,
            "type": "revenue_discrepancy",
            "title": "Revenue discrepancy found",
            "body": '"Midnight Dreams" expected $3,450, received $1,200. Potential underpayment of $2,250.',
            "priority": NotificationPriority.HIGH,
            "extra_data": {"song_id": 1, "expected": 3450, "received": 1200, "amount": 2250, "platform": "Spotify"},
            "action_url": "/revenue",
            "created_at": datetime.now() - timedelta(hours=2),
        },
        {
            "category": NotificationCategory.ATTENTION,
            "type": "song_missing",
            "title": "Song missing in statement",
            "body": '"Summer Vibes" has 1.2M streams but was not reported in your DistroKid statement.',
            "priority": NotificationPriority.HIGH,
            "extra_data": {"song_id": 2, "streams": 1200000, "distributor": "DistroKid"},
            "action_url": "/revenue",
            "created_at": datetime.now() - timedelta(hours=5),
        },
        {
            "category": NotificationCategory.ATTENTION,
            "type": "revenue_leak",
            "title": "Revenue leak found",
            "body": '"Electric Nights" has registration issues with ASCAP. Estimated loss: $890/month.',
            "priority": NotificationPriority.HIGH,
            "extra_data": {"song_id": 3, "amount": 890, "pro": "ASCAP"},
            "action_url": "/catalog",
            "created_at": datetime.now() - timedelta(hours=8),
        },
        {
            "category": NotificationCategory.ATTENTION,
            "type": "termination_expiring",
            "title": "Termination window closing",
            "body": "You have 14 days to exercise your termination rights for the Sony/ATV publishing deal.",
            "priority": NotificationPriority.HIGH,
            "extra_data": {"agreement_id": 1, "days_remaining": 14, "publisher": "Sony/ATV"},
            "action_url": "/agreements",
            "created_at": datetime.now() - timedelta(days=1),
        },
        {
            "category": NotificationCategory.ATTENTION,
            "type": "audit_expiring",
            "title": "Audit window closing",
            "body": "Your right to audit Universal Music Publishing expires in 30 days.",
            "priority": NotificationPriority.MEDIUM,
            "extra_data": {"agreement_id": 2, "days_remaining": 30, "publisher": "Universal Music Publishing"},
            "action_url": "/agreements",
            "created_at": datetime.now() - timedelta(days=2),
        },
        {
            "category": NotificationCategory.ATTENTION,
            "type": "collection_ending",
            "title": "Post-term collection ending",
            "body": 'Warner Chappell stops collecting for "City Lights" on March 15, 2025.',
            "priority": NotificationPriority.MEDIUM,
            "extra_data": {"song_id": 4, "publisher": "Warner Chappell", "end_date": "2025-03-15"},
            "action_url": "/agreements",
            "created_at": datetime.now() - timedelta(days=3),
        },
        {
            "category": NotificationCategory.ATTENTION,
            "type": "unauthorized_use",
            "title": "Potential unauthorized use",
            "body": 'TuneScan detected "Starlight" in a YouTube video with 2.4M views. No license found.',
            "priority": NotificationPriority.HIGH,
            "extra_data": {"song_id": 5, "platform": "YouTube", "views": 2400000},
            "action_url": "/tunescan",
            "created_at": datetime.now() - timedelta(hours=12),
        },
    ]

    # Feed items (informational) - matching the spec
    feed_notifications = [
        {
            "category": NotificationCategory.FEED,
            "type": "new_match",
            "title": "New match detected",
            "body": '"Midnight Dreams" found on TikTok - licensed use confirmed.',
            "extra_data": {"song_id": 1, "platform": "TikTok", "licensed": True},
            "action_url": "/tunescan",
            "created_at": datetime.now() - timedelta(minutes=30),
        },
        {
            "category": NotificationCategory.FEED,
            "type": "approaching_recoupment",
            "title": "Almost recouped",
            "body": '"Summer Vibes" is 92% recouped. Only $1,450 remaining.',
            "extra_data": {"song_id": 2, "percentage": 92, "remaining": 1450},
            "action_url": "/revenue",
            "created_at": datetime.now() - timedelta(hours=2),
        },
        {
            "category": NotificationCategory.FEED,
            "type": "revenue_spike",
            "title": "Revenue spike detected",
            "body": '"Electric Nights" earned $4,250 this week - 340% above your average.',
            "extra_data": {"song_id": 3, "amount": 4250, "percentage_increase": 340},
            "action_url": "/revenue",
            "created_at": datetime.now() - timedelta(hours=4),
        },
        {
            "category": NotificationCategory.FEED,
            "type": "weekly_report",
            "title": "Weekly report ready",
            "body": "Week of Dec 15: 2.4M streams, $8,920 revenue across all platforms.",
            "extra_data": {"streams": 2400000, "revenue": 8920, "week": "Dec 15"},
            "action_url": "/revenue",
            "created_at": datetime.now() - timedelta(hours=6),
        },
        {
            "category": NotificationCategory.FEED,
            "type": "milestone",
            "title": "Milestone reached!",
            "body": '"Midnight Dreams" just hit 10 million streams on Spotify!',
            "extra_data": {"song_id": 1, "milestone": 10000000, "platform": "Spotify"},
            "action_url": "/catalog",
            "created_at": datetime.now() - timedelta(hours=12),
        },
        {
            "category": NotificationCategory.FEED,
            "type": "trending",
            "title": "Trending now",
            "body": '"Summer Vibes" streams up 215% in the last 48 hours.',
            "extra_data": {"song_id": 2, "percentage_increase": 215, "period": "48h"},
            "action_url": "/catalog",
            "created_at": datetime.now() - timedelta(days=1),
        },
        {
            "category": NotificationCategory.FEED,
            "type": "statement_processed",
            "title": "Revenue statement processed",
            "body": "Your Spotify Q4 2024 statement has been imported. 287 transactions added.",
            "extra_data": {"transactions": 287, "distributor": "Spotify", "quarter": "Q4 2024"},
            "action_url": "/revenue",
            "created_at": datetime.now() - timedelta(days=1, hours=6),
        },
        {
            "category": NotificationCategory.FEED,
            "type": "new_match_auto",
            "title": "Auto-scan complete",
            "body": "Daily scan found 3 new uses of your catalog across streaming platforms.",
            "extra_data": {"matches_found": 3},
            "action_url": "/tunescan",
            "created_at": datetime.now() - timedelta(days=2),
        },
        {
            "category": NotificationCategory.FEED,
            "type": "catalog_update",
            "title": "Catalog synced",
            "body": "Your catalog has been synced with Apple Music. 2 new tracks detected.",
            "extra_data": {"tracks_added": 2, "platform": "Apple Music"},
            "action_url": "/catalog",
            "created_at": datetime.now() - timedelta(days=3),
        },
        {
            "category": NotificationCategory.FEED,
            "type": "statement_processed",
            "title": "Apple Music statement ready",
            "body": "Your Apple Music Q3 royalty statement has been imported. $3,450 in new earnings.",
            "extra_data": {"distributor": "Apple Music", "amount": 3450},
            "action_url": "/revenue",
            "created_at": datetime.now() - timedelta(days=5),
        },
    ]

    # Insert all notifications
    all_notifications = attention_notifications + feed_notifications

    for notif_data in all_notifications:
        notification = Notification(
            user_id=user.id,
            status=NotificationStatus.UNREAD,
            **notif_data
        )
        session.add(notification)

    session.commit()
    print(f"Created {len(attention_notifications)} attention items and {len(feed_notifications)} feed items.")
    session.close()

if __name__ == "__main__":
    seed_notifications()
