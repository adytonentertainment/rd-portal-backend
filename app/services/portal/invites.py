"""Portal access invites (infra PRD §7.2, Dropbox-style sharing).

An invite grants ONE email access to ONE writer. Tokens are single-use and
stored only as a sha256 hash. Accepting an invite finds-or-creates the
Contact and the login User, links them to the writer, and returns both so the
caller can mint a session.
"""

from __future__ import annotations

import hashlib
import re
import secrets
from datetime import datetime, timedelta
from typing import List, Optional, Tuple

from sqlalchemy.orm import Session

from app.models.models import User
from app.models.statements import (
    Contact,
    ContactRole,
    PortalInvite,
    Writer,
    WriterContact,
)

INVITE_TTL_DAYS = 14


def _hash(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


def _unique_username(db: Session, email: str) -> str:
    base = re.sub(r"[^a-z0-9]+", "_", email.lower().split("@")[0]).strip("_") or "writer"
    candidate = base
    n = 1
    while db.query(User).filter(User.username == candidate).first() is not None:
        n += 1
        candidate = f"{base}_{n}"
    return candidate


def writer_ids_for_contact(db: Session, contact: Contact) -> List[int]:
    return [
        wc.writer_id
        for wc in db.query(WriterContact).filter(WriterContact.contact_id == contact.id)
    ]


def contact_for_user(db: Session, user: User) -> Optional[Contact]:
    return db.query(Contact).filter(Contact.user_id == user.id).first()


def create_invite(
    db: Session,
    writer_id: int,
    email: str,
    role: ContactRole = ContactRole.MANAGER,
    invited_by_user_id: Optional[int] = None,
    is_admin_invite: bool = False,
) -> Tuple[PortalInvite, str]:
    """Create a pending invite; returns (invite, raw_token). The raw token is
    shown once (embedded in the link) and never stored. Any prior active
    invite for the same (writer, email) is revoked so only one link works."""
    email = email.strip().lower()
    now = datetime.now()

    if db.get(Writer, writer_id) is None:
        raise ValueError("writer not found")

    # Only reject when this email already has a *claimed login* on this writer —
    # then an invite is a no-op. A recorded contact with no login (user_id is
    # None) is exactly who we want to invite, so a bare WriterContact link is
    # not a blocker; the invite grants them the login they don't have yet.
    existing_contact = db.query(Contact).filter(Contact.email == email).first()
    if existing_contact is not None and existing_contact.user_id is not None:
        already = (
            db.query(WriterContact)
            .filter(
                WriterContact.writer_id == writer_id,
                WriterContact.contact_id == existing_contact.id,
            )
            .first()
        )
        if already is not None:
            raise ValueError("email already has access to this writer")

    for prior in (
        db.query(PortalInvite)
        .filter(PortalInvite.writer_id == writer_id, PortalInvite.email == email)
        .all()
    ):
        if prior.is_active(now):
            prior.revoked_at = now

    raw = secrets.token_urlsafe(32)
    invite = PortalInvite(
        writer_id=writer_id,
        email=email,
        token_hash=_hash(raw),
        role=role,
        invited_by_user_id=invited_by_user_id,
        is_admin_invite=is_admin_invite,
        expires_at=now + timedelta(days=INVITE_TTL_DAYS),
    )
    db.add(invite)
    db.commit()
    return invite, raw


def get_active_invite(db: Session, raw_token: str) -> Optional[PortalInvite]:
    inv = (
        db.query(PortalInvite)
        .filter(PortalInvite.token_hash == _hash(raw_token))
        .first()
    )
    if inv is None or not inv.is_active(datetime.now()):
        return None
    return inv


def revoke_invite(db: Session, invite_id: int) -> None:
    inv = db.get(PortalInvite, invite_id)
    if inv is None:
        raise ValueError("invite not found")
    if inv.is_active(datetime.now()):
        inv.revoked_at = datetime.now()
        db.commit()


class InviteAuthRequired(Exception):
    """The invite is valid but the caller has not proven they own the account."""


def accept_invite(
    db: Session,
    raw_token: str,
    password: Optional[str],
    bcrypt_context,
    authenticated_email: Optional[str] = None,
) -> Tuple[Contact, User, PortalInvite]:
    """Redeem a token: find/create the Contact + login User, link them to the
    writer, mark the invite accepted. Returns (contact, user, invite).

    Holding the link is NOT proof of identity. The token says which mailbox was
    invited; it must never be enough to log in as an account that already
    exists — otherwise anyone who is forwarded (or finds in a log) an invite for
    an existing user mints a session as them, up to and including an admin.
    So:
      * email has no login  -> `password` creates one (the onboarding case)
      * email has a login   -> the caller must prove ownership, either by
        already being authenticated as that same email (covers Google/OAuth
        accounts, which have no local password) or by supplying that account's
        current password.
    """
    inv = get_active_invite(db, raw_token)
    if inv is None:
        raise ValueError("invalid or expired invite")

    email = inv.email
    contact = db.query(Contact).filter(Contact.email == email).first()
    if contact is None:
        contact = Contact(email=email)
        db.add(contact)
        db.flush()

    user = db.query(User).filter(User.email == email).first()
    if user is None:
        if not password:
            raise ValueError("password required to create a login")
        user = User(
            email=email,
            username=_unique_username(db, email),
            hashed_password=bcrypt_context.hash(password),
            activated=True,
            royalty_per_stream=0,
        )
        db.add(user)
        db.flush()
    else:
        already_signed_in = bool(
            authenticated_email
            and authenticated_email.strip().lower() == (email or "").strip().lower()
        )
        if not already_signed_in:
            if not password:
                raise InviteAuthRequired(
                    "This email already has an account. Sign in to accept the invite."
                )
            if not user.hashed_password:
                # OAuth-only account: there is no password to check, so the only
                # acceptable proof is being signed in as that account.
                raise InviteAuthRequired(
                    "This account signs in with Google. Sign in first, then open "
                    "the invite link."
                )
            try:
                valid = bcrypt_context.verify(password, user.hashed_password)
            except Exception:  # malformed/legacy hash must never authenticate
                valid = False
            if not valid:
                raise InviteAuthRequired("Incorrect password for this account.")
    contact.user_id = user.id

    link = (
        db.query(WriterContact)
        .filter(
            WriterContact.writer_id == inv.writer_id,
            WriterContact.contact_id == contact.id,
        )
        .first()
    )
    if link is None:
        db.add(WriterContact(writer_id=inv.writer_id, contact_id=contact.id, role=inv.role))

    inv.accepted_at = datetime.now()
    db.commit()
    return contact, user, inv
