"""Durable counters for login throttling and account lockout.

Both controls previously lived in a Python dict on the running process, which
meant they were erased by every deploy and every container restart — and Render
restarts routinely. An attacker did not need to outwait a 30-minute lockout;
they needed to wait for the next deploy, or simply keep going until one
happened. The limits read as protection in code review and were close to
nothing in practice.

Postgres rather than Redis because the database already exists and this volume
is trivial: one small upsert per failed login, none on success beyond a delete.
Adding an instance to the stack for a counter would be the more expensive
mistake.

One row per (scope, identifier):
  scope      what is being limited — 'login', 'signup', 'lockout'
  identifier the client IP, or the username/email for lockout
"""

from datetime import datetime

from sqlalchemy import Column, DateTime, Integer, String, UniqueConstraint

from ..database import Base


class AuthThrottle(Base):
    __tablename__ = "auth_throttle"
    __table_args__ = (
        UniqueConstraint("scope", "identifier", name="uq_auth_throttle_scope_identifier"),
    )

    id = Column(Integer, primary_key=True)
    scope = Column(String(32), nullable=False, index=True)
    identifier = Column(String(320), nullable=False)  # max email length

    # Attempts inside the current window. Reset rather than decayed: a window
    # that has expired is simply started again.
    count = Column(Integer, nullable=False, default=0)
    window_start = Column(DateTime, nullable=False, default=datetime.now)

    # Set once the threshold is passed. Null means "counting, not locked".
    locked_until = Column(DateTime, nullable=True)

    updated_at = Column(DateTime, nullable=False, default=datetime.now, onupdate=datetime.now)
