# Database Migration Quick Start

## First Time Setup

1. **Install Alembic** (if not already installed):
   ```bash
   pip install -r requirements.txt
   ```

2. **Apply the initial migration** to add the `release_date` column:
   ```bash
   # On Linux/Mac:
   ./scripts/migrate.sh

   # On Windows PowerShell:
   .\scripts\migrate.ps1

   # Or manually:
   cd tunescan_backend
   python -m alembic -c migrations/alembic.ini upgrade head
   ```

## Common Tasks

### Apply Pending Migrations
```bash
./scripts/migrate.sh          # Linux/Mac
.\scripts\migrate.ps1          # Windows
```

### Create a New Migration
```bash
# Linux/Mac:
./scripts/create_migration.sh "add user avatar column"

# Windows:
.\scripts\create_migration.ps1 "add user avatar column"
```

### View Migration Status
```bash
cd tunescan_backend
python -m alembic -c migrations/alembic.ini current      # Show current version
python -m alembic -c migrations/alembic.ini history      # Show all migrations
```

### Rollback Last Migration
```bash
cd tunescan_backend
python -m alembic -c migrations/alembic.ini downgrade -1
```

## Server Deployment

### For Production Server (Ubuntu)

1. **SSH into your server**
2. **Navigate to backend directory**:
   ```bash
   cd /root/tunescan_backend_development
   ```

3. **Activate virtual environment**:
   ```bash
   source env/bin/activate
   ```

4. **Pull latest code**:
   ```bash
   git pull
   ```

5. **Install/update dependencies**:
   ```bash
   pip install -r requirements.txt
   ```

6. **Run migrations**:
   ```bash
   python -m alembic -c migrations/alembic.ini upgrade head
   ```

7. **Restart the backend service**:
   ```bash
   sudo systemctl restart tunescan_backend
   ```

## Troubleshooting

### Error: "Can't locate revision identified by 'xyz'"
This means your database is out of sync. You may need to:
1. Check current version: `python -m alembic -c migrations/alembic.ini current`
2. View migration history: `python -m alembic -c migrations/alembic.ini history`
3. If needed, stamp to current: `python -m alembic -c migrations/alembic.ini stamp head`

### Error: "Target database is not up to date"
Just run: `python -m alembic -c migrations/alembic.ini upgrade head`

### Error: Column already exists
The migration checks for this and will skip safely. This is expected if you manually added the column.

## Files Structure

```
tunescan_backend/
├── migrations/
│   ├── alembic.ini                      # Alembic configuration
│   ├── env.py                           # Migration environment setup
│   ├── script.py.mako                   # Migration template
│   ├── README.md                        # Full documentation
│   ├── QUICKSTART.md                    # This file
│   └── versions/
│       └── 001_add_release_date_to_catalog.py
└── scripts/
    ├── migrate.sh                       # Apply migrations (Linux/Mac)
    ├── migrate.ps1                      # Apply migrations (Windows)
    ├── create_migration.sh              # Create migration (Linux/Mac)
    └── create_migration.ps1             # Create migration (Windows)
```
