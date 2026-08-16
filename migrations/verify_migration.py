"""
Quick verification script to check the migrated database.

Usage:
    From backend root folder:
    python migrations/verify_migration.py
"""

import os
import sys

# Set environment
os.environ['ENVIRONMENT'] = 'DEVELOPMENT'

# Add current directory to path so we can import app modules
sys.path.insert(0, '.')

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from app.models.models import User, Subscription

# Connect to the migrated database
DATABASE_URL = "sqlite:///./tunescan_migrated.db"

engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
db = SessionLocal()

print("="*70)
print("MIGRATION DATABASE VERIFICATION")
print("="*70)

try:
    # Count records
    user_count = db.query(User).count()
    subscription_count = db.query(Subscription).count()

    print(f"\nDatabase: {DATABASE_URL}")
    print(f"Total Users: {user_count}")
    print(f"Total Subscriptions: {subscription_count}")

    # List all users with their subscriptions
    print("\n" + "="*70)
    print("USER AND SUBSCRIPTION DETAILS")
    print("="*70)

    users = db.query(User).all()
    for user in users:
        subscription = user.get_active_subscription(stripe_mode='live')

        print(f"\nUser #{user.id}")
        print(f"  Email: {user.email}")
        print(f"  Username: {user.username}")
        print(f"  Activated: {user.activated}")
        print(f"  Has Password: {user.hashed_password is not None}")
        print(f"  Stripe Customer ID: {user.stripe_customer_id}")

        if subscription:
            print(f"  Subscription:")
            print(f"    - ID: {subscription.subscription_id}")
            print(f"    - Tier: {subscription.tier.value}")
            print(f"    - Scans: {subscription.scans}")
            print(f"    - Mode: {subscription.stripe_mode}")
        else:
            print(f"  [WARNING] No active subscription found!")

    # Verify all subscriptions are in live mode
    print("\n" + "="*70)
    print("SUBSCRIPTION MODE CHECK")
    print("="*70)

    subscriptions = db.query(Subscription).all()
    live_count = sum(1 for s in subscriptions if s.stripe_mode == 'live')
    test_count = sum(1 for s in subscriptions if s.stripe_mode == 'test')

    print(f"Live mode subscriptions: {live_count}")
    print(f"Test mode subscriptions: {test_count}")

    if test_count > 0:
        print("[WARNING] Found test mode subscriptions!")
    else:
        print("[OK] All subscriptions are in live mode")

    # Verify all are Essential tier
    print("\n" + "="*70)
    print("TIER VERIFICATION")
    print("="*70)

    from app.models.models import SubscriptionTier
    essential_count = sum(1 for s in subscriptions if s.tier == SubscriptionTier.ESSENTIAL)

    print(f"Essential tier subscriptions: {essential_count}")
    print(f"Other tier subscriptions: {subscription_count - essential_count}")

    if essential_count == subscription_count:
        print("[OK] All subscriptions are TuneMGMT Essential tier")
    else:
        print("[WARNING] Not all subscriptions are Essential tier!")

    print("\n" + "="*70)
    print("VERIFICATION COMPLETE")
    print("="*70)
    print("\nSummary:")
    print(f"  - {user_count} users migrated")
    print(f"  - {subscription_count} subscriptions created")
    print(f"  - All subscriptions in live mode: {test_count == 0}")
    print(f"  - All subscriptions Essential tier: {essential_count == subscription_count}")
    print(f"  - All users activated: {all(u.activated for u in users)}")
    print(f"  - Users need password reset: {all(u.hashed_password is None for u in users)}")
    print("="*70)

finally:
    db.close()
