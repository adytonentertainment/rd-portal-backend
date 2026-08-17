"""Send the portal invite email, and record whether it arrived.

Two rules shape this module:

1. **A send failure must never fail the invite.** The token is already created
   and valid; the admin can copy the link out of the UI regardless. If SMTP is
   down, losing the invite too would be strictly worse. So every failure is
   caught and written to the invite as `delivery_status='failed'` with the
   reason, and the endpoint still returns 201.

2. **The raw token is a bearer credential.** It goes into the email body and
   nowhere else — never into a log line, never into an error message. Anyone
   holding it can claim that writer's portal.

Sending runs in a FastAPI BackgroundTask so a slow SMTP handshake (this server
does STARTTLS + login on every message) does not hold the request open.
"""

from __future__ import annotations

from datetime import datetime
from urllib.parse import urljoin

from app.database.database import SessionLocal
from app.emails.emails import EMail
from app.logger.logger import get_logger
from app.models.statements import Contact, PortalInvite, Publisher, Writer
from app.settings import get_settings

logger = get_logger("portal_invites")


def invite_url(raw_token: str) -> str:
    """The link the recipient clicks. Points at the FRONTEND accept page
    (/invite/:token), not the API — the API would return raw JSON."""
    base = get_settings().base_url_frontend or ""
    if not base.endswith("/"):
        base += "/"
    return urljoin(base, f"invite/{raw_token}")


def send_invite_email(invite_id: int, raw_token: str) -> bool:
    """Deliver the invite and record the outcome. Returns True on success.

    Safe to call from a background task: it opens its own session, because the
    request's session is already closed by the time this runs.
    """
    db = SessionLocal()
    try:
        invite = db.get(PortalInvite, invite_id)
        if invite is None:
            logger.warning(f"invite {invite_id} vanished before its email was sent")
            return False

        writer = db.get(Writer, invite.writer_id)
        writer_name = writer.canonical_name if writer else "your catalog"

        # Name the actual publisher, not the software. This mail arrives cold
        # and asks the reader to click through to their earnings; the one thing
        # that makes it credible is the name they already do business with.
        publisher = db.get(Publisher, writer.publisher_id) if writer else None
        sender = publisher.name if publisher else None

        # The recipient's own preference wins over the writer's: one contact
        # (a manager, an attorney) can represent several writers, and it is the
        # reader who has to understand the mail, not the catalog.
        contact = (
            db.query(Contact).filter(Contact.email == invite.email).first()
        )
        language = (
            (contact.preferred_language if contact else None)
            or (writer.preferred_language if writer else None)
            or "en"
        )

        try:
            EMail().send_portal_invite_email(
                recipient_email=invite.email,
                writer_name=writer_name,
                accept_url=invite_url(raw_token),
                expires_at=invite.expires_at,
                invited_by=sender,
                language=language,
            )
        except Exception as exc:
            # Keep the reason, drop anything token-shaped: the message can
            # echo the recipient and server, never the credential.
            invite.delivery_status = "failed"
            invite.delivery_error = str(exc)[:500].replace(raw_token, "<token>")
            db.commit()
            logger.error(
                f"invite {invite_id} email to {invite.email} failed: {type(exc).__name__}"
            )
            return False

        invite.delivery_status = "sent"
        invite.delivery_error = None
        invite.sent_at = datetime.now()
        db.commit()
        logger.info(f"invite {invite_id} emailed to {invite.email}")
        return True
    finally:
        db.close()
