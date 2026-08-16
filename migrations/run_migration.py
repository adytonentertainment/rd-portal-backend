"""
Simple script to run database migrations.

This script will:
1. Check the current migration state
2. Apply any pending migrations
3. Show the final migration state

Usage:
    From backend root folder:
    python migrations/run_migration.py
"""

import os
import sys
from alembic.config import Config
from alembic import command

# Path to alembic.ini
ALEMBIC_INI_PATH = "migrations/alembic.ini"

def run_migrations():
    """Run all pending migrations."""
    print("="*70)
    print("DATABASE MIGRATION SCRIPT")
    print("="*70)

    # Create Alembic config
    alembic_cfg = Config(ALEMBIC_INI_PATH)

    # Show current version
    print("\nCurrent migration state:")
    try:
        command.current(alembic_cfg, verbose=True)
    except Exception as e:
        print(f"Could not get current state: {e}")

    print("\n" + "="*70)
    print("APPLYING MIGRATIONS")
    print("="*70)

    # Run migrations
    try:
        command.upgrade(alembic_cfg, "head")
        print("\n[OK] Migrations applied successfully!")
    except Exception as e:
        print(f"\n[ERROR] Error applying migrations: {e}")
        import traceback
        traceback.print_exc()
        return False

    # Show new version
    print("\n" + "="*70)
    print("NEW MIGRATION STATE")
    print("="*70)
    try:
        command.current(alembic_cfg, verbose=True)
    except Exception as e:
        print(f"Could not get new state: {e}")

    print("\n" + "="*70)
    print("MIGRATION COMPLETE")
    print("="*70)
    print("Your database is now up to date!")

    return True

if __name__ == "__main__":
    success = run_migrations()
    sys.exit(0 if success else 1)
