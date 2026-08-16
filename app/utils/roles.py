"""Verax role helpers — effective-admin resolution and the bootstrap allowlist.

Kept dependency-free (no router imports) so both the auth register endpoint and
the require_admin dependency can share it without circular imports.

Admin security model:
  - A user is an *effective* admin only when role == 'admin' AND admin_approved.
  - Self-registered admins land pending (admin_approved = False) until an
    existing admin approves them.
  - Bootstrap: emails in the ADMIN_EMAILS env var are always effective admins
    (so the first admin can get in, and existing deployments keep working). New
    admin registrations whose email is in ADMIN_EMAILS are auto-approved.
"""

import os


def bootstrap_admin_emails() -> set:
    """Emails auto-trusted as admins via the ADMIN_EMAILS env var (comma-sep)."""
    raw = os.getenv("ADMIN_EMAILS", "")
    return {e.strip().lower() for e in raw.split(",") if e.strip()}


def is_bootstrap_admin_email(email: str) -> bool:
    return bool(email) and email.lower() in bootstrap_admin_emails()


def is_effective_admin(user) -> bool:
    """True only for an approved admin (or a bootstrap-allowlisted email)."""
    if user is None or not getattr(user, "email", None):
        return False
    if is_bootstrap_admin_email(user.email):
        return True
    return getattr(user, "role", None) == "admin" and bool(getattr(user, "admin_approved", False))
