"""
Pre-Launch Email Signup Router
Handles email collection for pre-launch waitlist
"""

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError
from pydantic import BaseModel, EmailStr
from datetime import datetime

from app.database.session import get_session
from app.models.models import PreLaunchSignup

prelaunch_router = APIRouter(prefix="/prelaunch", tags=["PreLaunch"])


class EmailSignupRequest(BaseModel):
    email: EmailStr


class EmailSignupResponse(BaseModel):
    success: bool
    message: str
    email: str


class SignupDetailsRequest(BaseModel):
    email: EmailStr
    name: str = None
    company: str = None
    catalog_size: str = None
    role: str = None
    interest: str = None
    notes: str = None


@prelaunch_router.post("/signup", response_model=EmailSignupResponse, status_code=status.HTTP_201_CREATED)
async def signup_email(
    request: Request,
    signup_request: EmailSignupRequest,
    db: Session = Depends(get_session)
):
    """
    Add email to pre-launch waitlist

    - **email**: Valid email address

    Returns success message or error if email already exists
    """

    # Get client information
    ip_address = request.client.host if request.client else None
    user_agent = request.headers.get("user-agent", None)

    try:
        # Check if email already exists
        existing = db.query(PreLaunchSignup).filter(
            PreLaunchSignup.email == signup_request.email
        ).first()

        if existing:
            return EmailSignupResponse(
                success=True,
                message="You're already on the waitlist!",
                email=signup_request.email
            )

        # Create new signup
        new_signup = PreLaunchSignup(
            email=signup_request.email,
            created_at=datetime.now(),
            ip_address=ip_address,
            user_agent=user_agent
        )

        db.add(new_signup)
        db.commit()
        db.refresh(new_signup)

        return EmailSignupResponse(
            success=True,
            message="Thanks! We'll notify you when we launch.",
            email=signup_request.email
        )

    except IntegrityError:
        db.rollback()
        # Race condition - email was added by another request
        return EmailSignupResponse(
            success=True,
            message="You're already on the waitlist!",
            email=signup_request.email
        )
    except Exception as e:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to process signup. Please try again."
        )


@prelaunch_router.patch("/signup/details")
async def update_signup_details(
    details: SignupDetailsRequest,
    db: Session = Depends(get_session),
):
    signup = db.query(PreLaunchSignup).filter(
        PreLaunchSignup.email == details.email
    ).first()

    if not signup:
        raise HTTPException(status_code=404, detail="Email not found")

    if details.name is not None:
        signup.name = details.name
    if details.company is not None:
        signup.company = details.company
    if details.catalog_size is not None:
        signup.catalog_size = details.catalog_size
    if details.role is not None:
        signup.role = details.role
    if details.interest is not None:
        signup.interest = details.interest
    if details.notes is not None:
        signup.notes = details.notes

    db.commit()
    return {"success": True, "message": "Details saved"}