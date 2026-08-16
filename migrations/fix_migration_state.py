"""
Script to fix migration state when the database schema is ahead of migration tracking.

This happens when:
- Columns were added manually (not through migrations)
- Database was created from fresh models
- Migration history got out of sync

This script will:
1. Check which columns exist in the database
2. Stamp the database to the correct migration version
3. Then run any remaining migrations

Usage:
    From backend root folder:
    python migrations/fix_migration_state.py
"""

import os
import sys
import sqlite3
from alembic.config import Config
from alembic import command

# Set environment
os.environ['ENVIRONMENT'] = 'DEVELOPMENT'

# Path to database and alembic.ini
DATABASE_PATH = "tunescan_development.db"
ALEMBIC_INI_PATH = "migrations/alembic.ini"

def check_column_exists(table_name, column_name):
    """Check if a column exists in a table."""
    conn = sqlite3.connect(DATABASE_PATH)
    cursor = conn.cursor()

    cursor.execute(f"PRAGMA table_info({table_name})")
    columns = [row[1] for row in cursor.fetchall()]

    conn.close()
    return column_name in columns

def main():
    print("="*70)
    print("MIGRATION STATE FIX SCRIPT")
    print("="*70)
    print(f"\nDatabase: {DATABASE_PATH}")

    # Check which columns exist
    print("\nChecking database schema...")

    # Check Subscription table for stripe_mode (migration 005)
    has_stripe_mode = check_column_exists('Subscription', 'stripe_mode')
    print(f"  Subscription.stripe_mode exists: {has_stripe_mode}")

    # Check User table for profile fields (migration 006)
    has_first_name = check_column_exists('User', 'first_name')
    has_last_name = check_column_exists('User', 'last_name')
    has_ipi_number = check_column_exists('User', 'ipi_number')
    has_avatar_url = check_column_exists('User', 'avatar_url')

    print(f"  User.first_name exists: {has_first_name}")
    print(f"  User.last_name exists: {has_last_name}")
    print(f"  User.ipi_number exists: {has_ipi_number}")
    print(f"  User.avatar_url exists: {has_avatar_url}")

    # Create Alembic config
    alembic_cfg = Config(ALEMBIC_INI_PATH)

    # Determine which migration to stamp to
    if has_stripe_mode and not has_first_name:
        # Database has migration 005 applied but not tracked
        print("\n" + "="*70)
        print("FIXING: Database has migration 005 applied but not tracked")
        print("="*70)
        print("\nStamping database to migration 005...")
        command.stamp(alembic_cfg, "005")
        print("Database stamped to migration 005")

        print("\nNow applying migration 006...")
        command.upgrade(alembic_cfg, "head")

    elif has_first_name:
        # All migrations already applied
        print("\n" + "="*70)
        print("FIXING: All columns exist but migration tracking is behind")
        print("="*70)
        print("\nStamping database to migration 006 (head)...")
        command.stamp(alembic_cfg, "head")
        print("Database stamped to migration 006")

    else:
        # Database is at migration 004, need to apply 005 and 006
        print("\n" + "="*70)
        print("Database is correctly at migration 004")
        print("="*70)
        print("\nApplying migrations 005 and 006...")
        command.upgrade(alembic_cfg, "head")

    # Show final state
    print("\n" + "="*70)
    print("FINAL MIGRATION STATE")
    print("="*70)
    command.current(alembic_cfg, verbose=True)

    print("\n" + "="*70)
    print("MIGRATION STATE FIXED!")
    print("="*70)
    print("Your database is now in sync with migrations.")

if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"\n[ERROR] {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
