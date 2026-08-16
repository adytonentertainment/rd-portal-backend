#!/bin/bash
# Statement ingest worker for the live-test setup. Polls the DB and drives each
# upload through sort -> parse -> validate -> done in the background, so a large
# drop never blocks the API. Must point at the SAME DB + storage as the backend.
cd /Users/stevengarcia/VERAX_2/verax_backend
source venv/bin/activate 2>/dev/null
export ENVIRONMENT=DEVELOPMENT
export SQLALCHEMY_DATABASE_URL="sqlite:////Users/stevengarcia/VERAX_2/verax_backend/verax_livetest.db"
export INGEST_POLL_SECONDS=2
exec python3 scripts/run_ingest_worker.py
