"""Verax account creation + admin approval (role-based auth).

Registration is role-aware:
  - role='writer'  → created immediately, ready to use.
  - role='admin'   → requires a valid ADMIN_SIGNUP_CODE to even create the
                     account, and lands PENDING (admin_approved=False) unless the
                     email is in the ADMIN_EMAILS bootstrap allowlist. A pending
                     admin can log in but is refused by every admin route until
                     an existing admin approves them.

This is separate from the legacy `/auth/user` signup (ACRCloud/Stripe/beta) —
Verax accounts don't need that machinery.
"""

import os
from datetime import datetime, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from jose import jwt
from pydantic import BaseModel, EmailStr, field_validator
from sqlalchemy.orm import Session
from starlette import status

from app.database.session import get_session
from app.logger.logger import get_logger
from app.middleware.rate_limit import check_rate_limit, signup_rate_limiter
from app.models.models import User
from app.routers.auth import ALGORITHM, SECRET_KEY, bcrypt_context, get_user
from app.routers.statements_admin import require_admin
from app.utils.password_validator import validate_password
from app.utils.roles import is_bootstrap_admin_email, is_effective_admin

logger = get_logger("accounts")

accounts_router = APIRouter(prefix="/auth", tags=["Accounts"])
admin_accounts_router = APIRouter(prefix="/admin/accounts", tags=["Admin Accounts"])


class RegisterRequest(BaseModel):
    email: EmailStr
    username: str
    password: str
    role: str = "writer"  # 'writer' | 'admin'
    admin_code: Optional[str] = None

    @field_validator("username")
    @classmethod
    def _username(cls, v):
        if not v or len(v) < 3 or len(v) > 30:
            raise ValueError("Username must be 3–30 characters")
        return v

    @field_validator("role")
    @classmethod
    def _role(cls, v):
        if v not in ("writer", "admin"):
            raise ValueError("role must be 'writer' or 'admin'")
        return v

    @field_validator("password")
    @classmethod
    def _password(cls, v):
        ok, errors = validate_password(v)
        if not ok:
            raise ValueError("; ".join(errors))
        return v


def _mint_token(user: User) -> str:
    payload = {
        "sub": user.username,
        "id": user.id,
        "email": user.email,
        "exp": datetime.now() + timedelta(days=7),
    }
    return jwt.encode(payload, key=SECRET_KEY, algorithm=ALGORITHM)


def _user_out(user: User) -> dict:
    effective_admin = is_effective_admin(user)
    return {
        "id": user.id,
        "email": user.email,
        "username": user.username,
        "role": user.role,
        "admin_approved": bool(user.admin_approved),
        # what the account can actually DO right now
        "is_admin": effective_admin,
        "pending_admin_approval": user.role == "admin" and not effective_admin,
    }


@accounts_router.post("/register", status_code=status.HTTP_201_CREATED)
async def register(
    body: RegisterRequest,
    request: Request,
    db: Session = Depends(get_session),
):
    """Create a Verax admin or writer account."""
    await check_rate_limit(request, signup_rate_limiter)

    email = body.email.lower()
    if db.query(User).filter(User.email == email).first():
        raise HTTPException(status_code=409, detail="Email already registered")
    if db.query(User).filter(User.username == body.username).first():
        raise HTTPException(status_code=409, detail="Username already taken")

    admin_approved = False
    if body.role == "admin":
        # Gate 1 — a valid signup code is required to create ANY admin account
        # (even a pending one). Absent/misconfigured code => refuse.
        expected = os.getenv("ADMIN_SIGNUP_CODE")
        if not expected or body.admin_code != expected:
            raise HTTPException(
                status_code=403,
                detail="A valid admin signup code is required to create an admin account.",
            )
        # Gate 2 — bootstrap emails are auto-approved; everyone else is pending
        # until an existing admin approves them.
        admin_approved = is_bootstrap_admin_email(email)

    user = User(
        email=email,
        username=body.username,
        hashed_password=bcrypt_context.hash(body.password),
        role=body.role,
        admin_approved=admin_approved,
        activated=True,
        royalty_per_stream=0,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    logger.info(
        f"registered {body.role} account {user.id} ({email}); "
        f"admin_approved={admin_approved}"
    )
    return {"access_token": _mint_token(user), "token_type": "bearer", **_user_out(user)}


@accounts_router.get("/me/account")
async def my_account(user: User = Depends(get_user)):
    """The caller's own account + effective role (for the frontend to route)."""
    return _user_out(user)


# --- admin approval workflow (approved admins only) --------------------------


@admin_accounts_router.get("/admins")
async def list_admins(
    pending: Optional[bool] = None,
    _admin: User = Depends(require_admin),
    db: Session = Depends(get_session),
):
    """All admin accounts, optionally filtered to those pending approval."""
    q = db.query(User).filter(User.role == "admin")
    admins = q.order_by(User.id).all()
    rows = [
        {
            "id": u.id,
            "email": u.email,
            "username": u.username,
            "admin_approved": bool(u.admin_approved),
            "effective": is_effective_admin(u),
        }
        for u in admins
    ]
    if pending is True:
        rows = [r for r in rows if not r["effective"]]
    return {"admins": rows}


@admin_accounts_router.post("/admins/{user_id}/approve")
async def approve_admin(
    user_id: int,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_session),
):
    """Approve a pending admin account. Auditable via logs."""
    target = db.get(User, user_id)
    if target is None:
        raise HTTPException(status_code=404, detail="User not found")
    if target.role != "admin":
        raise HTTPException(status_code=409, detail="User is not an admin account")
    target.admin_approved = True
    db.commit()
    logger.info(f"admin {admin.id} approved admin account {target.id} ({target.email})")
    return _user_out(target)


@admin_accounts_router.post("/admins/{user_id}/revoke")
async def revoke_admin(
    user_id: int,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_session),
):
    """Revoke an admin's approval (they keep the account, lose admin access).
    Bootstrap ADMIN_EMAILS addresses can't be revoked this way."""
    target = db.get(User, user_id)
    if target is None:
        raise HTTPException(status_code=404, detail="User not found")
    if target.id == admin.id:
        raise HTTPException(status_code=409, detail="You cannot revoke your own admin access")
    if is_bootstrap_admin_email(target.email):
        raise HTTPException(
            status_code=409, detail="Bootstrap (ADMIN_EMAILS) admins cannot be revoked here"
        )
    target.admin_approved = False
    db.commit()
    logger.info(f"admin {admin.id} revoked admin access for {target.id} ({target.email})")
    return _user_out(target)
