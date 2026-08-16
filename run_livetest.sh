#!/bin/bash
# Live-test backend: isolated seeded DB + admin allowlist. Statement ingestion
# runs in a SEPARATE worker (run_worker.sh), NOT inline — a big drop (~2,600
# files) would otherwise block the request thread and hang the server.
# (Does NOT touch tunescan_development.db.) Re-seed anytime with: python seed_livetest.py
cd /Users/stevengarcia/VERAX_2/verax_backend
source venv/bin/activate 2>/dev/null
export ENVIRONMENT=DEVELOPMENT
export ADMIN_EMAILS="steven@adytonentertainment.com,steven@verax.app,demo@demo.local"
export SQLALCHEMY_DATABASE_URL="sqlite:////Users/stevengarcia/VERAX_2/verax_backend/verax_livetest.db"
export ADMIN_SIGNUP_CODE="123"  # code for the /register admin path (not needed to log in)
# NOTE: INGEST_INLINE is intentionally NOT set — run run_worker.sh alongside.
exec python3 -m uvicorn app.main:app --host 0.0.0.0 --port 8000
