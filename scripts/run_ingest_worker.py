#!/usr/bin/env python
"""Standalone DB-polled statement ingest worker (PRD §12.1: no Celery/Redis).

Polls for in-flight statement uploads and advances each one stage step at a
time (uploaded -> sorting -> parsing -> validating -> done/failed); sleeps
when there is nothing to do.

Usage:
    ENVIRONMENT=DEVELOPMENT venv/bin/python scripts/run_ingest_worker.py

Env:
    INGEST_POLL_SECONDS  idle sleep between polls (default 5)
"""

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.database import SessionLocal  # noqa: E402
from app.logger.logger import get_logger  # noqa: E402
from app.services.statement_ingest.worker import run_pending_jobs  # noqa: E402

logger = get_logger("ingest_worker")

POLL_SECONDS = float(os.getenv("INGEST_POLL_SECONDS", "5"))


def main() -> None:
    logger.info(f"Statement ingest worker started (poll every {POLL_SECONDS}s)")
    while True:
        session = SessionLocal()
        try:
            steps = run_pending_jobs(session)
        except Exception:
            # advance_upload contains per-upload failures; this guards the
            # poll query itself (e.g. transient DB outage) — log and retry.
            logger.exception("Worker poll iteration failed")
            steps = 0
        finally:
            session.close()
        if steps == 0:
            time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
