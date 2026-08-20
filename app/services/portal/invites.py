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


def _unique_username(db: Session, seed: str) -> str:
    base = re.sub(r"[^a-z0-9]+", "_", (seed or "").lower().split("@")[0]).strip("_") or "writer"
    candidate = base
    n = 1
    while db.query(User).filter(User.username == candidate).first() is not None:
        n += 1
        candidate = f"{base}_{n}"
    return candidate


def suggested_username(db: Session, writer_name: str) -> str:
    """What the claim form offers, before the person edits it.

    Seeded from the CLIENT's name rather than the mailbox, because the username
    is what distinguishes one portal from another when the same manager holds
    several — "amenazzy" and "canserbero" tell them apart at the login screen;
    two variations on their own address do not.
    """
    return _unique_username(db, writer_name)


class UsernameTaken(Exception):
    """The chosen username belongs to somebody already."""


def writer_ids_for_user(db: Session, user: User) -> List[int]:
    """The clients THIS LOGIN claimed.

    Identity is per client. A manager who represents three writers claims three
    portals from the same mailbox, each with its own username and password, and
    signing into one must show that client and nothing else — so the claim is
    read off `writer_contact.user_id`, never off the address.
    """
    return sorted(
        wc.writer_id
        for wc in db.query(WriterContact).filter(WriterContact.user_id == user.id)
    )


def writer_ids_for_contact(db: Session, contact: Contact) -> List[int]:
    """Clients reachable by the signed-in claim.

    The portal resolves this from the login (see `current_contact`, which
    stashes the answer on the instance it returns). The fallback below is for
    callers holding only an address — an admin screen, a test — where "which
    login" is not a question that has been asked yet; it reports the clients
    that address has actually claimed, never the ones it is merely listed on.
    """
    stashed = getattr(contact, "_claim_writer_ids", None)
    if stashed is not None:
        return stashed
    return sorted(
        wc.writer_id
        for wc in db.query(WriterContact).filter(
            WriterContact.contact_id == contact.id,
            WriterContact.user_id.isnot(None),
        )
    )


def contact_for_user(db: Session, user: User) -> Optional[Contact]:
    """The address book entry behind this login.

    Looked up through the claim: one mailbox now backs several logins, so
    `Contact.user_id` can only ever name one of them and is no longer the
    authority on who is signed in.
    """
    link = (
        db.query(WriterContact).filter(WriterContact.user_id == user.id).first()
    )
    if link is not None:
        return db.get(Contact, link.contact_id)
    # Legacy claim made before identity moved onto the link.
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

    # Reject only when THIS client's portal has already been claimed by this
    # address — then the invite is a no-op. Read off the link, not the contact:
    # `Contact.user_id` names whichever client this address claimed first, so
    # judging by it refused to invite a manager to their second client just
    # because they had claimed their first.
    existing_contact = db.query(Contact).filter(Contact.email == email).first()
    if existing_contact is not None:
        already = (
            db.query(WriterContact)
            .filter(
                WriterContact.writer_id == writer_id,
                WriterContact.contact_id == existing_contact.id,
                WriterContact.user_id.isnot(None),
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
    username: Optional[str] = None,
) -> Tuple[Contact, User, PortalInvite]:
    """Redeem a token: create the login for THIS client and mark it accepted.

    Identity is per client, not per mailbox. Every acceptance mints its own
    login — its own username, its own password — and attaches it to the
    (writer, contact) link. The same manager claiming three writers ends up
    with three logins from one inbox, and signing into any one of them shows
    that client alone.

    That also removes a whole class of takeover: an invite never touches an
    existing account, so being forwarded a link cannot mint a session as
    somebody who already has one. The link is only ever good for the client it
    names, and only once.

    `username` defaults to the client's name and is the identity someone signs
    in with; it must be free, or UsernameTaken is raised for the caller to ask
    again.
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

    writer = db.get(Writer, inv.writer_id)

    existing = (
        db.query(WriterContact)
        .filter(
            WriterContact.writer_id == inv.writer_id,
            WriterContact.contact_id == contact.id,
            WriterContact.user_id.isnot(None),
        )
        .first()
    )
    if existing is not None:
        raise ValueError("this client's portal has already been claimed by that email")

    if not password:
        raise ValueError("password required to create a login")

    chosen = (username or "").strip()
    if chosen:
        clash = db.query(User).filter(User.username == chosen).first()
        if clash is not None:
            raise UsernameTaken(
                f"The username {chosen!r} is taken. Pick another."
            )
    else:
        chosen = suggested_username(db, writer.canonical_name if writer else email)

    user = User(
        email=email,
        username=chosen,
        hashed_password=bcrypt_context.hash(password),
        activated=True,
        royalty_per_stream=0,
    )
    db.add(user)
    db.flush()

    # Keep the address book pointing at a login for backwards compatibility;
    # the authority on who claimed what is the link below.
    if contact.user_id is None:
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
        link = WriterContact(
            writer_id=inv.writer_id, contact_id=contact.id, role=inv.role
        )
        db.add(link)
        db.flush()
    # THE claim. Everything the portal scopes on reads this, so a login only
    # ever reaches the client it was created for.
    link.user_id = user.id

    inv.accepted_at = datetime.now()
    db.commit()
    return contact, user, inv
