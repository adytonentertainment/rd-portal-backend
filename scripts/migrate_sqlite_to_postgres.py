"""
SQLite to PostgreSQL Migration Script

This script migrates all data from a SQLite database to PostgreSQL.
It handles the schema differences and preserves relationships.

Usage:
    python scripts/migrate_sqlite_to_postgres.py --sqlite-path ./database.db --postgres-url postgresql://user:pass@host:port/dbname

Requirements:
    - Both databases should have the same schema (run alembic migrations on PostgreSQL first)
    - PostgreSQL database should be empty (or data will be appended)
"""

import argparse
import sqlite3
from datetime import datetime
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
import json
import psycopg2
from psycopg2.extras import Json

# Register JSON adapter for psycopg2
psycopg2.extensions.register_adapter(dict, Json)
psycopg2.extensions.register_adapter(list, Json)


def get_sqlite_connection(sqlite_path: str):
    """Create SQLite connection"""
    conn = sqlite3.connect(sqlite_path)
    conn.row_factory = sqlite3.Row
    return conn


def get_postgres_engine(postgres_url: str):
    """Create PostgreSQL engine"""
    engine = create_engine(postgres_url)
    return engine


def get_table_names(sqlite_conn):
    """Get all table names from SQLite"""
    cursor = sqlite_conn.cursor()
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' AND name NOT LIKE 'alembic_%';")
    return [row[0] for row in cursor.fetchall()]


def get_table_data(sqlite_conn, table_name: str):
    """Get all data from a SQLite table"""
    cursor = sqlite_conn.cursor()
    cursor.execute(f'SELECT * FROM "{table_name}"')
    columns = [description[0] for description in cursor.description]
    rows = cursor.fetchall()
    return columns, rows


def migrate_table(sqlite_conn, pg_engine, table_name: str, dry_run: bool = False):
    """Migrate a single table from SQLite to PostgreSQL"""
    columns, rows = get_table_data(sqlite_conn, table_name)

    if not rows:
        print(f"  [SKIP] {table_name}: No data to migrate")
        return 0

    print(f"  [MIGRATE] {table_name}: {len(rows)} rows")

    if dry_run:
        return len(rows)

    # Known boolean columns in the schema
    boolean_columns = {
        'User': ['activated'],
        'Subscription': ['limit_exceeded'],
        'UserCatalog': ['is_infringement'],
    }

    with pg_engine.connect() as conn:
        for row in rows:
            # Build column list and values
            values = {}
            for i, col in enumerate(columns):
                value = row[i]

                # Handle JSON columns
                if isinstance(value, str) and (value.startswith('{') or value.startswith('[')):
                    try:
                        value = json.loads(value)
                    except json.JSONDecodeError:
                        pass

                # Handle boolean columns (SQLite stores as 0/1, PostgreSQL needs True/False)
                if table_name in boolean_columns and col in boolean_columns[table_name]:
                    if value is not None:
                        value = bool(value)

                # Handle None values
                if value is None:
                    values[col] = None
                else:
                    values[col] = value

            # Build INSERT statement
            col_names = ', '.join([f'"{c}"' for c in columns])
            placeholders = ', '.join([f':{c}' for c in columns])

            insert_sql = text(f'INSERT INTO "{table_name}" ({col_names}) VALUES ({placeholders}) ON CONFLICT DO NOTHING')

            try:
                conn.execute(insert_sql, values)
            except Exception as e:
                print(f"    [ERROR] Failed to insert row in {table_name}: {e}")
                print(f"    [DEBUG] Values: {values}")
                raise

        conn.commit()

    return len(rows)


def reset_sequences(pg_engine, table_names: list):
    """Reset PostgreSQL sequences to max(id) + 1 for each table"""
    print("\n[SEQUENCES] Resetting PostgreSQL sequences...")

    with pg_engine.connect() as conn:
        for table_name in table_names:
            try:
                # Check if table has an 'id' column with a sequence
                result = conn.execute(text(f"""
                    SELECT column_default
                    FROM information_schema.columns
                    WHERE table_name = '{table_name}'
                    AND column_name = 'id'
                """))
                row = result.fetchone()

                if row and row[0] and 'nextval' in str(row[0]):
                    # Get sequence name from column default
                    seq_name = str(row[0]).split("'")[1]

                    # Get max id
                    max_result = conn.execute(text(f'SELECT COALESCE(MAX(id), 0) FROM "{table_name}"'))
                    max_id = max_result.fetchone()[0]

                    # Reset sequence
                    conn.execute(text(f"SELECT setval('{seq_name}', {max_id + 1}, false)"))
                    print(f"  [OK] {table_name}: sequence reset to {max_id + 1}")

            except Exception as e:
                print(f"  [SKIP] {table_name}: {e}")

        conn.commit()


def migrate_database(sqlite_path: str, postgres_url: str, dry_run: bool = False):
    """Main migration function"""
    print("=" * 60)
    print("SQLite to PostgreSQL Migration")
    print("=" * 60)
    print(f"\nSource: {sqlite_path}")
    print(f"Target: {postgres_url.split('@')[1] if '@' in postgres_url else postgres_url}")
    print(f"Dry Run: {dry_run}")
    print()

    # Connect to databases
    sqlite_conn = get_sqlite_connection(sqlite_path)
    pg_engine = get_postgres_engine(postgres_url)

    # Get table names
    table_names = get_table_names(sqlite_conn)
    print(f"Found {len(table_names)} tables to migrate:")
    for t in table_names:
        print(f"  - {t}")
    print()

    # Define migration order (respecting foreign key constraints)
    # Tables with no foreign keys first, then dependent tables
    migration_order = [
        "User",
        "Songs",
        "Subscription",
        "UserCatalog",
        "ACRCloudScan",
        "StatsCache",
        "StatsSyncMetadata",
        "RevenueStatement",
        "RevenueTransaction",
    ]

    # Add any tables not in the predefined order
    for table in table_names:
        if table not in migration_order:
            migration_order.append(table)

    # Filter to only existing tables
    migration_order = [t for t in migration_order if t in table_names]

    print("[MIGRATION] Starting migration...")
    total_rows = 0

    for table_name in migration_order:
        try:
            rows = migrate_table(sqlite_conn, pg_engine, table_name, dry_run)
            total_rows += rows
        except Exception as e:
            print(f"  [ERROR] Failed to migrate {table_name}: {e}")
            raise

    # Reset sequences if not dry run
    if not dry_run:
        reset_sequences(pg_engine, migration_order)

    print()
    print("=" * 60)
    print(f"Migration {'preview' if dry_run else 'completed'}!")
    print(f"Total rows {'to migrate' if dry_run else 'migrated'}: {total_rows}")
    print("=" * 60)

    sqlite_conn.close()


def main():
    parser = argparse.ArgumentParser(description='Migrate SQLite database to PostgreSQL')
    parser.add_argument('--sqlite-path', required=True, help='Path to SQLite database file')
    parser.add_argument('--postgres-url', required=True, help='PostgreSQL connection URL')
    parser.add_argument('--dry-run', action='store_true', help='Preview migration without making changes')

    args = parser.parse_args()

    migrate_database(args.sqlite_path, args.postgres_url, args.dry_run)


if __name__ == '__main__':
    main()
