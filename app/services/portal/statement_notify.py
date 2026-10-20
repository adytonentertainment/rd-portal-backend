"""Tell writers' portal contacts that new statements are ready.

Publishing makes statements visible in the portal; it never emails anyone.
Notifying is a separate, deliberate admin action for the NEWEST RELEASE:

* **The newest release** is every period ending in the latest month that has
  published statements — e.g. H1 2026 and, for the quarterly accounts, Q2 2026
  (both end in June). It is the only thing that can be notified, so uploading
  or publishing older (backfilled) data never produces an email.
* **Once per writer per release.** A writer whose contacts were told is done
  for the release; statements that arrive later (a late batch, a correction)
  simply appear in their portal.
* **Once per person per release.** A contact already emailed in this release is
  never emailed again in it, even about another of their writers.
* **One email per person per run**, naming all their writers, in their
  language, with no amounts (see EMail.send_statements_ready_email).
* **Only people who can sign in**: a contact counts once they have accepted an
  invite for that client (writer_contact.user_id), the portal's own access
  rule. Everyone else is reported as waiting for a login.

Sending is paced and runs in the background, like the bulk invite sender, and
each recipient's outcome is kept in `statement_notification`.
"""

from __future__ import annotations

import re
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
# Rows that did not reach the person and may be retried.
UNSENT = ("failed", "not_sent", "suppressed")
# In flight: queued, or claimed by a sender.
IN_FLIGHT = ("pending", "sending")


def _retryable(codes: List[str]):
    """Unsent rows, plus in-flight rows abandoned by a restart mid-send."""
    from sqlalchemy import and_, or_

    stale = datetime.now() - STALE_RUN
    return and_(
        StatementNotification.period_code.in_(codes),
        or_(
            StatementNotification.status.in_(UNSENT),
            and_(StatementNotification.status.in_(IN_FLIGHT), StatementNotification.created_at < stale),
        ),
    )


class NotifyRefused(Exception):
    """The request cannot be honoured; the message is safe to show the admin."""


def portal_statements_url() -> str:
    base = get_settings().base_url_frontend or ""
    if not base.endswith("/"):
        base += "/"
    return urljoin(base, "statements")


def period_label(code: str) -> str:
    g = re.search(r"PUB(\d{2})([QH]\d)", code or "")
    return f"{g.group(2)} 20{g.group(1)}" if g else (code or "")


def newest_release(db: Session) -> List[str]:
    """Period codes ending in the latest month with published statements,
    widest first (H1 2026 before Q2 2026). Empty when nothing is published."""
    parsed = {}
    for (code,) in (
        db.query(Distribution.period_code).filter(Distribution.portal_visible.is_(True)).distinct()
    ):
        p = parse_period(code)
        if p is not None:
            parsed[code] = p  # (year, month_start, month_end)
    if not parsed:
        return []
    latest_end = max((p[0], p[2]) for p in parsed.values())
    codes = [c for c, p in parsed.items() if (p[0], p[2]) == latest_end]
    return sorted(codes, key=lambda c: parsed[c][1])  # earliest start = widest


def notifiable_periods(db: Session) -> Set[str]:
    return set(newest_release(db))


def release_label(codes: List[str]) -> str:
    return " + ".join(period_label(c) for c in codes)


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


def _release_state(db: Session, codes: List[str]):
    """(pending, notified_writers, codes_by_writer) for the release.

    pending: writer_id -> un-announced distribution ids, for writers not yet
    told in this release. A writer told once is done for the release.
    """
    rows = (
        db.query(Distribution.id, Distribution.writer_id, Distribution.notified_at, Distribution.period_code)
        .filter(Distribution.period_code.in_(codes), Distribution.portal_visible.is_(True))
        .all()
    )
    eligible = _eligible_writer_ids(db, {r[1] for r in rows})
    notified = {w for _, w, n, _ in rows if n is not None and w in eligible}
    pending: Dict[int, List[int]] = defaultdict(list)
    codes_by_writer: Dict[int, Set[str]] = defaultdict(set)
    for dist_id, w, n, code in rows:
        if w not in eligible:
            continue
        codes_by_writer[w].add(code)
        if w not in notified and n is None:
            pending[w].append(dist_id)
    return dict(pending), notified, codes_by_writer


def _emailed_contacts(db: Session, codes: List[str]) -> Set[int]:
    return {
        cid
        for (cid,) in db.query(StatementNotification.contact_id).filter(
            StatementNotification.period_code.in_(codes),
            StatementNotification.status == "sent",
            StatementNotification.contact_id.isnot(None),
        )
    }


def plan(db: Session):
    """Who would be emailed now for the newest release.

    Returns (codes, recipients, without_login, covered):
      recipients: [{contact, writer_ids, distribution_ids, period_code}]
      without_login: writer ids nobody can sign in for yet
      covered: writer_id -> distribution ids whose every contact was already
               emailed in this release (marked done without another email)
    """
    codes = newest_release(db)
    if not codes:
        return codes, [], [], {}
    pending, _, codes_by_writer = _release_state(db, codes)
    if not pending:
        return codes, [], [], {}
    links = (
        db.query(WriterContact)
        .filter(WriterContact.writer_id.in_(list(pending.keys())), WriterContact.user_id.isnot(None))
        .all()
    )
    already = _emailed_contacts(db, codes)
    by_contact: Dict[int, Set[int]] = defaultdict(set)
    has_login: Set[int] = set()
    reachable: Set[int] = set()
    for link in links:
        has_login.add(link.writer_id)
        if link.contact_id in already:
            continue
        by_contact[link.contact_id].add(link.writer_id)
        reachable.add(link.writer_id)
    contacts = (
        {c.id: c for c in db.query(Contact).filter(Contact.id.in_(list(by_contact.keys())))}
        if by_contact
        else {}
    )
    order = {c: i for i, c in enumerate(codes)}
    recipients = []
    for contact_id, writer_ids in sorted(by_contact.items()):
        contact = contacts.get(contact_id)
        if contact is None or not contact.email:
            continue
        ids = sorted(writer_ids)
        their_codes = {c for w in ids for c in codes_by_writer[w]}
        recipients.append(
            {
                "contact": contact,
                "writer_ids": ids,
                "distribution_ids": sorted(d for w in ids for d in pending[w]),
                # Wording for the email: the widest period they hold (a half
                # year covers its quarter).
                "period_code": min(their_codes, key=lambda c: order[c]),
            }
        )
    without_login = sorted(w for w in pending if w not in has_login)
    covered = {w: pending[w] for w in has_login if w not in reachable}
    return codes, recipients, without_login, covered


def _sending(db: Session, codes: List[str]) -> bool:
    if not codes:
        return False
    cutoff = datetime.now() - STALE_RUN
    return (
        db.query(StatementNotification.id)
        .filter(
            StatementNotification.period_code.in_(codes),
            StatementNotification.status.in_(IN_FLIGHT),
            StatementNotification.created_at >= cutoff,
        )
        .first()
        is not None
    )


def summary(db: Session) -> dict:
    """What the Notify dialog shows before and after a run."""
    codes, recipients, without_login, _ = plan(db)
    notified_writers: Set[int] = set()
    all_writers: Set[int] = set()
    if codes:
        pending, notified_writers, codes_by_writer = _release_state(db, codes)
        all_writers = set(codes_by_writer)
    counts: Dict[str, int] = defaultdict(int)
    last = None
    if codes:
        for status, created_at in db.query(
            StatementNotification.status, StatementNotification.created_at
        ).filter(StatementNotification.period_code.in_(codes)):
            counts["pending" if status == "sending" else status] += 1
            last = max(last, created_at) if last else created_at
    return {
        "periods": codes,
        "label": release_label(codes),
        "recipients": len(recipients),
        "writers_to_notify": len({w for r in recipients for w in r["writer_ids"]}),
        "writers_without_login": len(without_login),
        "writers_notified": len(notified_writers),
        "writers_total": len(all_writers),
        "sending": _sending(db, codes),
        "emails": {k: counts.get(k, 0) for k in ("sent", "failed", "suppressed", "not_sent", "pending")},
        "last_run_at": last.isoformat() if last else None,
    }


def start(db: Session, admin_id: Optional[int]) -> List[int]:
    """Queue one notification per recipient for the newest release; returns
    the new row ids. Writers whose contacts were all already emailed in this
    release are marked done without another email."""
    codes, recipients, _, covered = plan(db)
    if not codes:
        raise NotifyRefused("Nothing has been published yet.")
    if _sending(db, codes):
        raise NotifyRefused("Notifications for this release are already being sent.")
    now = datetime.now()
    covered_ids = [d for ids in covered.values() for d in ids]
    if covered_ids:
        for dist in db.query(Distribution).filter(Distribution.id.in_(covered_ids)):
            dist.notified_at = now
    if not recipients:
        db.commit()
        if covered_ids:
            return []
        raise NotifyRefused("Everyone who can sign in has already been notified for this release.")
    writers = {
        w.id: w
        for w in db.query(Writer).filter(Writer.id.in_([w for r in recipients for w in r["writer_ids"]]))
    }
    # A person held back or failed in an earlier run keeps ONE row for the
    # release: it is refreshed rather than duplicated, so a later retry can
    # never email them twice.
    unsent = defaultdict(list)
    for row in db.query(StatementNotification).filter(
        _retryable(codes),
        StatementNotification.contact_id.in_([r["contact"].id for r in recipients]),
    ):
        unsent[row.contact_id].append(row)
    ids = []
    for r in recipients:
        contact = r["contact"]
        first_writer = writers.get(r["writer_ids"][0])
        language = (
            contact.preferred_language
            or (first_writer.preferred_language if first_writer else None)
            or "en"
        )
        previous = sorted(unsent.get(contact.id, []), key=lambda x: x.id)
        for old in previous[:-1]:
            old.status = "replaced"
        row = previous[-1] if previous else StatementNotification(contact_id=contact.id, created_by=admin_id)
        row.period_code = r["period_code"]
        row.email = contact.email
        row.language = language
        row.writer_ids = r["writer_ids"]
        row.distribution_ids = r["distribution_ids"]
        row.status = "pending"
        row.error = None
        row.created_at = now
        if row.id is None:
            db.add(row)
            db.flush()
        ids.append(row.id)
    db.commit()
    logger.info(f"notify {release_label(codes)}: queued {len(ids)} email(s) by admin {admin_id}")
    return ids


def retry(db: Session) -> List[int]:
    """Re-queue the release's failed, unsent and allowlist-held emails."""
    codes = newest_release(db)
    if not codes:
        raise NotifyRefused("Nothing has been published yet.")
    if _sending(db, codes):
        raise NotifyRefused("Notifications for this release are already being sent.")
    already = _emailed_contacts(db, codes)
    rows = (
        db.query(StatementNotification)
        .filter(_retryable(codes))
        .order_by(StatementNotification.id)
        .all()
    )
    latest = {}
    for row in rows:
        if row.contact_id in already:
            row.status = "replaced"  # reached since by another run
            continue
        if row.contact_id in latest:
            latest[row.contact_id].status = "replaced"  # keep only the newest per person
        latest[row.contact_id] = row
    ids = []
    for row in latest.values():
        row.status = "pending"
        row.error = None
        row.created_at = datetime.now()
        ids.append(row.id)
    db.commit()
    return sorted(ids)


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


def _deliver(db: Session, row: StatementNotification, mailer: EMail, codes: List[str]) -> bool:
    # Final guarantee of "once per person per release", whatever queued this row.
    if row.contact_id is not None and (
        db.query(StatementNotification.id)
        .filter(
            StatementNotification.id != row.id,
            StatementNotification.contact_id == row.contact_id,
            StatementNotification.period_code.in_(codes or [row.period_code]),
            StatementNotification.status.in_(("sent", "sending")),
        )
        .first()
        is not None
    ):
        row.status = "replaced"
        row.error = "already emailed in this release"
        db.commit()
        return True
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
        codes = newest_release(db)
        for index, row_id in enumerate(row_ids):
            # Claim the row: only one sender can move it from pending to
            # sending, so a double-pressed Notify can never send it twice.
            claimed = (
                db.query(StatementNotification)
                .filter(StatementNotification.id == row_id, StatementNotification.status == "pending")
                .update({StatementNotification.status: "sending"}, synchronize_session=False)
            )
            db.commit()
            if not claimed:
                continue
            row = db.get(StatementNotification, row_id)
            db.refresh(row)
            if _deliver(db, row, mailer, codes):
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


def send_preview(db: Session, to_email: str, mailer: Optional[EMail] = None) -> dict:
    """Email the admin the English and Spanish versions of what writers will
    get for the newest release. Touches nothing: no rows, no notified_at."""
    codes, recipients, without_login, _ = plan(db)
    if not codes:
        raise NotifyRefused("Nothing has been published yet.")
    if recipients:
        writer_ids, period_code = recipients[0]["writer_ids"], recipients[0]["period_code"]
    else:
        writer_ids, period_code = without_login[:1], codes[0]
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
