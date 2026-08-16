"""In-process ingest runner.

The ingest pipeline is advanced by a polling worker. When that worker only
exists as `scripts/run_ingest_worker.py`, a deploy that forgets to start it
produces the worst possible failure: uploads are accepted, files are written to
disk, the API returns 202 — and nothing ever happens. No statements, no totals,
nothing distributable, and no error anywhere. The admin polls a progress screen
forever.

So the API starts a worker thread itself. It is safe to run alongside a
dedicated worker process: every upload is taken under an atomic DB lease
(worker._claim_upload), so only one runner ever advances a given upload, and a
crashed holder's lease expires and is retried.

Env:
    INGEST_WORKER_IN_PROCESS   "0" to disable (use when a dedicated worker runs)
    INGEST_POLL_SECONDS        idle sleep between polls (default 5)
"""

from __future__ import annotations

import os
import threading
import time

from app.logger.logger import get_logger

logger = get_logger("ingest_runner")

_thread: threading.Thread | None = None
_stop = threading.Event()


def _enabled() -> bool:
    return (os.getenv("INGEST_WORKER_IN_PROCESS", "1") or "1").strip().lower() not in (
        "0",
        "false",
        "no",
    )


def _loop(poll_seconds: float) -> None:
    # Imported lazily so importing this module never drags in the DB session.
    from app.database import SessionLocal
    from app.services.statement_ingest.worker import WORKER_ID, run_pending_jobs

    logger.info(f"In-process ingest worker started ({WORKER_ID}, poll {poll_seconds}s)")
    while not _stop.is_set():
        session = SessionLocal()
        try:
            steps = run_pending_jobs(session)
        except Exception:
            # Never let a bad poll kill the thread — an ingest worker that dies
            # silently is the exact failure this module exists to prevent.
            logger.exception("Ingest poll failed; retrying after backoff")
            steps = 0
        finally:
            session.close()
        if steps == 0:
            _stop.wait(poll_seconds)
    logger.info("In-process ingest worker stopped")


def start_ingest_worker() -> None:
    """Start the background worker unless it is explicitly disabled."""
    global _thread
    if not _enabled():
        logger.info(
            "In-process ingest worker disabled (INGEST_WORKER_IN_PROCESS=0) — a "
            "dedicated worker process must be running or uploads will never be "
            "processed"
        )
        return
    if _thread is not None and _thread.is_alive():
        return
    poll_seconds = float(os.getenv("INGEST_POLL_SECONDS", "5"))
    _stop.clear()
    _thread = threading.Thread(
        target=_loop, args=(poll_seconds,), name="ingest-worker", daemon=True
    )
    _thread.start()


def stop_ingest_worker(timeout: float = 5.0) -> None:
    """Signal the worker to finish its current poll and exit."""
    if _thread is None:
        return
    _stop.set()
    _thread.join(timeout=timeout)


def worker_status() -> dict:
    """Health for the admin surface: is anything actually going to run?"""
    return {
        "in_process_enabled": _enabled(),
        "in_process_running": bool(_thread is not None and _thread.is_alive()),
        "poll_seconds": float(os.getenv("INGEST_POLL_SECONDS", "5")),
    }
