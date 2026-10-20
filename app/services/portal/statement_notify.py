"""Tell writers' portal contacts that new statements are ready.

Publishing makes statements visible in the portal; it never emails anyone.
Notifying is a separate, deliberate admin action per reporting period:

* **Only the newest period can be notified** — the latest half-year, plus the
  latest quarter for the quarterly accounts. Uploading or publishing older
  (backfilled) data therefore never produces an email, not even by accident.
* **One email per person.** A contact holding several clients gets one email
  naming all of them, in their language.
* **Only people who can sign in.** A contact counts once they have accepted an
  invite for that client (writer_contact.user_id), the same rule the portal
  uses to grant access. Everyone else is reported as waiting for a login.
* **Never twice.** Each announced distribution gets `notified_at`; a second
  press only reaches statements published since.
* **No amounts in the email** (see EMail.send_statements_ready_email).

Sending is paced and runs in the background, like the bulk invite sender, and
each recipient's outcome is kept in `statement_notification`.
"""

from __future__ import annotations

import time
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Set
from urllib.parse import urljoin

from sqlalchemy.orm import Session

from app.database.database import SessionLocal
from app.emails.emails import EMail
from app.logger.logger import get_logger
from app.models.statements import (
    Contact,
    Distribution,
    Publisher,
    StatementNotification,
    Writer,
    WriterContact,
    WriterStatus,
)
from app.services.distribution.periods import parse_period
from app.services.portal.invite_delivery import MAX_CONSECUTIVE_FAILURES, SEND_PACING_SECONDS
from app.settings import get_settings

logger = get_logger("statement_notify")

# A run left 'pending' this long is assumed dead (process restarted mid-send)
# and no longer blocks a new one.
STALE_RUN = timedelta(minutes=30)


class NotifyRefused(Exception):
    """The request cannot be honoured; the message is safe to show the admin."""


def portal_statements_url() -> str:
    base = get_settings().base_url_frontend or ""
    if not base.endswith("/"):
        base += "/"
    return urljoin(base, "statements")


def notifiable_periods(db: Session) -> Set[str]:
    """The newest published half-year and the newest published quarter."""
    codes = [
        c
        for (c,) in db.query(Distribution.period_code)
        .filter(Distribution.portal_visible.is_(True))
        .distinct()
    ]
    latest: Dict[str, tuple] = {}
    for code in codes:
        parsed = parse_period(code)
        if parsed is None:
            continue
        kind = "Q" if "Q" in code[-2:] else "H"
        key = (parsed[0], parsed[2])  # year, month_end
        if kind not in latest or key > latest[kind][0]:
            latest[kind] = (key, code)
    return {code for _, code in latest.values()}


def _eligible_writer_ids(db: Session, writer_ids) -> Set[int]:
    """Clients whose contacts may be told: real, active, not acquired catalogs."""
    if not writer_ids:
        return set()
    return {
        w.id
        for w in db.query(Writer).filter(
            Writer.id.in_(list(writer_ids)),
            Writer.is_house_account.is_(False),
            Writer.publisher_owned.is_(False),
            Writer.status != WriterStatus.OFFBOARDED,
        )
    }


def _pending_by_writer(db: Session, period_code: str) -> Dict[int, List[int]]:
    """writer_id -> ids of visible, not-yet-announced distributions."""
    rows = (
        db.query(Distribution.id, Distribution.writer_id)
        .filter(
            Distribution.period_code == period_code,
            Distribution.portal_visible.is_(True),
            Distribution.notified_at.is_(None),
        )
        .all()
    )
    out: Dict[int, List[int]] = defaultdict(list)
    for dist_id, writer_id in rows:
        out[writer_id].append(dist_id)
    eligible = _eligible_writer_ids(db, out.keys())
    return {w: ids for w, ids in out.items() if w in eligible}


def plan(db: Session, period_code: str):
    """Who would be emailed now. Returns (recipients, writers_without_login).

    recipients: [{contact, writer_ids, distribution_ids}] — one per contact.
    """
    pending = _pending_by_writer(db, period_code)
    if not pending:
        return [], []
    links = (
        db.query(WriterContact)
        .filter(
            WriterContact.writer_id.in_(list(pending.keys())),
            WriterContact.user_id.isnot(None),
        )
        .all()
    )
    by_contact: Dict[int, Set[int]] = defaultdict(set)
    reached: Set[int] = set()
    for link in links:
        by_contact[link.contact_id].add(link.writer_id)
        reached.add(link.writer_id)
    contacts = {
        c.id: c for c in db.query(Contact).filter(Contact.id.in_(list(by_contact.keys())))
    } if by_contact else {}
    recipients = []
    for contact_id, writer_ids in sorted(by_contact.items()):
        contact = contacts.get(contact_id)
        if contact is None or not contact.email:
            continue
        ids = sorted(writer_ids)
        recipients.append(
            {
                "contact": contact,
                "writer_ids": ids,
                "distribution_ids": sorted(d for w in ids for d in pending[w]),
            }
        )
    without_login = sorted(w for w in pending if w not in reached)
    return recipients, without_login


def _sending(db: Session, period_code: str) -> bool:
    cutoff = datetime.now() - STALE_RUN
    return (
        db.query(StatementNotification.id)
        .filter(
            StatementNotification.period_code == period_code,
            StatementNotification.status == "pending",
            StatementNotification.created_at >= cutoff,
        )
        .first()
        is not None
    )


def summary(db: Session, period_code: str) -> dict:
    """What the Notify dialog shows before and after a run."""
    recipients, without_login = plan(db, period_code)
    visible = (
        db.query(Distribution.writer_id, Distribution.notified_at)
        .filter(Distribution.period_code == period_code, Distribution.portal_visible.is_(True))
        .all()
    )
    eligible = _eligible_writer_ids(db, {w for w, _ in visible})
    notified_writers = {w for w, n in visible if n is not None and w in eligible}
    pending_writers = {w for w, n in visible if n is None and w in eligible}
    counts: Dict[str, int] = defaultdict(int)
    last = None
    for status, created_at in db.query(
        StatementNotification.status, StatementNotification.created_at
    ).filter(StatementNotification.period_code == period_code):
        counts[status] += 1
        last = max(last, created_at) if last else created_at
    return {
        "period_code": period_code,
        "notifiable": period_code in notifiable_periods(db),
        "recipients": len(recipients),
        "writers_to_notify": len({w for r in recipients for w in r["writer_ids"]}),
        "writers_without_login": len(without_login),
        "writers_pending": len(pending_writers),
        "writers_notified": len(notified_writers),
        "sending": _sending(db, period_code),
        "emails": {k: counts.get(k, 0) for k in ("sent", "failed", "suppressed", "not_sent", "pending")},
        "last_run_at": last.isoformat() if last else None,
    }


def _check_notifiable(db: Session, period_code: str) -> None:
    if period_code not in notifiable_periods(db):
        raise NotifyRefused(
            "Only the newest reporting period can be notified. Older periods are "
            "historical, so uploading or publishing them never emails writers."
        )


def start(db: Session, period_code: str, admin_id: Optional[int]) -> List[int]:
    """Queue one notification per recipient; returns the new row ids."""
    _check_notifiable(db, period_code)
    if _sending(db, period_code):
        raise NotifyRefused("Notifications for this period are already being sent.")
    recipients, _ = plan(db, period_code)
    if not recipients:
        raise NotifyRefused("There is no one to notify for this period right now.")
    writers = {
        w.id: w
        for w in db.query(Writer).filter(
            Writer.id.in_([w for r in recipients for w in r["writer_ids"]])
        )
    }
    ids = []
    for r in recipients:
        contact = r["contact"]
        first_writer = writers.get(r["writer_ids"][0])
        language = (
            contact.preferred_language
            or (first_writer.preferred_language if first_writer else None)
            or "en"
        )
        row = StatementNotification(
            period_code=period_code,
            contact_id=contact.id,
            email=contact.email,
            language=language,
            writer_ids=r["writer_ids"],
            distribution_ids=r["distribution_ids"],
            status="pending",
            created_by=admin_id,
        )
        db.add(row)
        db.flush()
        ids.append(row.id)
    db.commit()
    logger.info(f"notify {period_code}: queued {len(ids)} email(s) by admin {admin_id}")
    return ids


def retry(db: Session, period_code: str) -> List[int]:
    """Re-queue the period's failed, unsent and allowlist-suppressed emails."""
    _check_notifiable(db, period_code)
    if _sending(db, period_code):
        raise NotifyRefused("Notifications for this period are already being sent.")
    rows = (
        db.query(StatementNotification)
        .filter(
            StatementNotification.period_code == period_code,
            StatementNotification.status.in_(("failed", "not_sent", "suppressed")),
        )
        .all()
    )
    for row in rows:
        row.status = "pending"
        row.error = None
        row.created_at = datetime.now()
    db.commit()
    return [r.id for r in rows]


def _render_args(db: Session, row: StatementNotification) -> dict:
    writers = (
        db.query(Writer).filter(Writer.id.in_(list(row.writer_ids))).order_by(Writer.canonical_name).all()
    )
    publisher = db.get(Publisher, writers[0].publisher_id) if writers else None
    return {
        "writer_names": [w.canonical_name for w in writers],
        "period_code": row.period_code,
        "portal_url": portal_statements_url(),
        "sender": publisher.name if publisher else None,
        "language": row.language,
    }


def _deliver(db: Session, row: StatementNotification, mailer: EMail) -> bool:
    if mailer._blocked_by_allowlist(row.email):
        # EMAIL_ALLOWLIST is on (a rollout test). Record it and leave the
        # statements un-announced, so a retry after lifting it still sends.
        row.status = "suppressed"
        row.error = "EMAIL_ALLOWLIST is active; not sent"
        db.commit()
        return True
    try:
        mailer.send_statements_ready_email(recipient_email=row.email, **_render_args(db, row))
    except Exception as exc:  # recorded, never raised: one bad address must not stop the run
        row.status = "failed"
        row.error = str(exc)[:500]
        db.commit()
        logger.error(f"statement notification {row.id} to {row.email} failed: {type(exc).__name__}")
        return False
    now = datetime.now()
    row.status = "sent"
    row.error = None
    row.sent_at = now
    for dist in db.query(Distribution).filter(
        Distribution.id.in_(list(row.distribution_ids)), Distribution.notified_at.is_(None)
    ):
        dist.notified_at = now
    db.commit()
    return True


def send(row_ids: List[int], mailer: Optional[EMail] = None, pacing: float = SEND_PACING_SECONDS) -> dict:
    """Deliver queued notifications, paced. Safe as a FastAPI background task:
    it opens its own session."""
    db = SessionLocal()
    mailer = mailer or EMail()
    sent = failed = consecutive = 0
    try:
        for index, row_id in enumerate(row_ids):
            row = db.get(StatementNotification, row_id)
            if row is None or row.status != "pending":
                continue
            if _deliver(db, row, mailer):
                sent += 1
                consecutive = 0
            else:
                failed += 1
                consecutive += 1
                if consecutive >= MAX_CONSECUTIVE_FAILURES:
                    reason = (
                        f"stopped after {MAX_CONSECUTIVE_FAILURES} consecutive failures — "
                        f"email looks misconfigured; retry once it is fixed"
                    )
                    for rest_id in row_ids[index + 1 :]:
                        rest = db.get(StatementNotification, rest_id)
                        if rest is not None and rest.status == "pending":
                            rest.status = "not_sent"
                            rest.error = reason
                    db.commit()
                    logger.error(f"statement notifications aborted after {index + 1} of {len(row_ids)}")
                    break
            if pacing and index + 1 < len(row_ids):
                time.sleep(pacing)
    finally:
        db.close()
    logger.info(f"statement notifications: {sent} delivered, {failed} failed")
    return {"sent": sent, "failed": failed}


def send_preview(db: Session, period_code: str, to_email: str, mailer: Optional[EMail] = None) -> dict:
    """Email the admin the English and Spanish versions of what writers will
    get. Touches nothing: no rows, no notified_at."""
    _check_notifiable(db, period_code)
    recipients, without_login = plan(db, period_code)
    if recipients:
        writer_ids = recipients[0]["writer_ids"]
    else:
        writer_ids = without_login[:1]
    writers = db.query(Writer).filter(Writer.id.in_(writer_ids)).all() if writer_ids else []
    names = [w.canonical_name for w in writers] or ["Example Writer"]
    publisher = db.get(Publisher, writers[0].publisher_id) if writers else None
    mailer = mailer or EMail()
    for language in ("en", "es"):
        mailer.send_statements_ready_email(
            recipient_email=to_email,
            writer_names=names,
            period_code=period_code,
            portal_url=portal_statements_url(),
            sender=publisher.name if publisher else None,
            language=language,
        )
    return {"sent_to": to_email, "languages": ["en", "es"], "example_writers": names}
