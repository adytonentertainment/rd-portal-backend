# Database Migrations

This directory contains Alembic database migrations for the TuneScan backend.

## Quick Start

### Install Alembic
```bash
pip install -r requirements.txt
```

### Common Commands

#### Create a new migration (auto-generate from model changes)
```bash
cd tunescan_backend
python -m alembic -c migrations/alembic.ini revision --autogenerate -m "description of changes"
```

#### Apply all pending migrations
```bash
cd tunescan_backend
python -m alembic -c migrations/alembic.ini upgrade head
```

#### Rollback one migration
```bash
cd tunescan_backend
python -m alembic -c migrations/alembic.ini downgrade -1
```

#### View migration history
```bash
cd tunescan_backend
python -m alembic -c migrations/alembic.ini history
```

#### View current database version
```bash
cd tunescan_backend
python -m alembic -c migrations/alembic.ini current
```

## Helper Scripts

Located in `scripts/` directory:

- **`migrate.sh`** / **`migrate.ps1`**: Apply all pending migrations
- **`create_migration.sh`** / **`create_migration.ps1`**: Create a new migration

## Migration Files

All migration files are stored in `migrations/versions/`. Each file contains:
- `upgrade()`: Changes to apply
- `downgrade()`: How to undo the changes

## Important Notes

1. Always review auto-generated migrations before applying them
2. Test migrations on a backup database first
3. Never edit migration files after they've been applied to production
4. Always create a database backup before running migrations in production
