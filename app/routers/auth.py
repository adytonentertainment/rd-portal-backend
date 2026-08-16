import os
import time
from datetime import datetime, timedelta
from typing import Literal, Union
from urllib.parse import urljoin

import requests
import stripe
from app.database.session import get_session
from app.emails import get_email_client
from app.logger import get_logger
from app.middleware.account_lockout import account_lockout
from app.middleware.rate_limit import (
    check_rate_limit,
    login_rate_limiter,
    password_reset_limiter,
    signup_rate_limiter,
)
from app.misc import royalty_dict
from app.models.models import (
    ACRCloudScan,
    Agreement,
    BatchUpload,
    Client,
    DeviceToken,
    Notification,
    PublishingAgreement,
    PushSubscription,
    RevenueStatement,
    RevenueTransaction,
    Subscription,
    User,
)
from app.schemas.auth import (
    AuthRequest,
    NewPassword,
    ResetPassword,
    ResetPasswordEmail,
    Token,
)
from app.schemas.user import CreateUserRequest, UpdateUserRequest
from app.settings.settings import get_settings
from app.utils.password_validator import MIN_LENGTH, get_password_requirements
from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from jose import JWTError, jwt
from passlib.context import CryptContext
from requests import RequestException
from sqlalchemy.orm import Session
from starlette import status

logger = get_logger()
settings = get_settings()
email_client = get_email_client()
auth_s = URLSafeTimedSerializer(settings.secret_key, "auth")

# put this in env file
SECRET_KEY = settings.secret_key
ALGORITHM = settings.secret_algorithm

auth_router = APIRouter(prefix="/auth", tags=["Authentication"])


@auth_router.get("/password-requirements")
async def get_password_requirements_endpoint():
    """
    Get password requirements for client-side validation.
    Returns the requirements that passwords must meet.
    """
    return {
        "requirements": get_password_requirements(),
        "min_length": MIN_LENGTH,
        "rules": [
            {"id": "length", "description": f"At least {MIN_LENGTH} characters"},
            {"id": "uppercase", "description": "At least one uppercase letter (A-Z)"},
            {"id": "lowercase", "description": "At least one lowercase letter (a-z)"},
            {"id": "number", "description": "At least one number (0-9)"},
            {
                "id": "special",
                "description": "At least one special character (!@#$%^&*()_+-=[]{}|;':\",./<>?~`)",
            },
        ],
    }


bcrypt_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
oauth2_bearer = OAuth2PasswordBearer(tokenUrl="auth/token", auto_error=False)


async def get_user(
    token: str = Depends(oauth2_bearer), db: Session = Depends(get_session)
):
    if token is None:
        raise HTTPException(status_code=401, detail="Authentication required")
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        username: str = payload.get("sub")
        if username is None:
            raise HTTPException(status_code=403, detail="Token is invalid or expired")
        user = db.query(User).filter(User.username == username).first()
        if not user:
            logger.warning(f"Token valid but user not found: {username}")
            raise HTTPException(status_code=403, detail="Token is invalid or expired")
        return user
    except JWTError:
        # Expected for expired/invalid tokens - no need to log
        raise HTTPException(status_code=403, detail="Token is invalid or expired")


@auth_router.post("/user", status_code=status.HTTP_201_CREATED)
async def create_user(
    request_body: CreateUserRequest,
    request: Request,
    db: Session = Depends(get_session),
):
    # Rate limiting
    await check_rate_limit(request, signup_rate_limiter)
    """
    Creates an ACRCloud container as well as a Stripe customer and adds
    the user to the database.
    **Important**
    Beta signup limits can be configured via BETA_SIGNUP_LIMIT env variable.
    Beta passphrase requirement can be disabled via BETA_REQUIRE_PASSPHRASE env variable.
    Captcha validation is not necessary when in development mode.
    """

    # Check beta signup limit
    beta_limit = getattr(settings, "beta_signup_limit", None)
    if beta_limit is not None and beta_limit > 0:
        user_count = db.query(User).count()
        if user_count >= beta_limit:
            raise HTTPException(
                status_code=400,
                detail="Beta signup limit reached. Please check back later.",
            )

    # Check passphrase requirement (configurable for beta)
    beta_require_passphrase = getattr(settings, "beta_require_passphrase", True)
    if (
        settings.mode == "development"
        and beta_require_passphrase
        and request_body.password != settings.passphrase
    ):
        raise HTTPException(status_code=400, detail="Registrations are disabled.")

    # check captcha
    if not request_body.captchaToken and settings.mode != "development":
        raise HTTPException(
            status_code=400,
            detail="An unexpected error occured, please try again later.",
        )
    if (
        not validate_captcha(request_body.captchaToken)
        and settings.mode != "development"
    ):
        raise HTTPException(
            status_code=400,
            detail="An unexpected error occured, please try again later.",
        )

    # Check if username already exists
    existing_username = (
        db.query(User).filter(User.username == request_body.username).first()
    )
    if existing_username:
        raise HTTPException(status_code=400, detail="This username is already taken.")

    # Check if email already exists
    existing_email = db.query(User).filter(User.email == request_body.email).first()
    if existing_email:
        raise HTTPException(status_code=400, detail="This email is already registered.")

    # Create stripe customer
    try:
        customer = stripe.Customer.create(
            name=request_body.username,
            email=request_body.email,
            description=request_body.username,
            test_clock=(
                stripe.test_helpers.TestClock.create(frozen_time=int(time.time()))
                if os.environ.get("env") == "development"
                else None
            ),
        )
    except stripe.error.InvalidRequestError as e:
        logger.error(f"Stripe error during signup: {e}")
        raise HTTPException(status_code=400, detail="Invalid email address format.")
    except stripe.error.StripeError as e:
        logger.error(f"Stripe error during signup: {e}")
        raise HTTPException(
            status_code=500,
            detail="Payment service temporarily unavailable. Please try again.",
        )

    user = User(
        username=request_body.username,
        email=request_body.email,
        hashed_password=bcrypt_context.hash(request_body.password),
        stripe_customer_id=customer["id"],
        activated=False,
        royalty_per_stream=royalty_dict["Worldwide"],
    )

    # Send email for account activation
    try:
        email_client.send_register_email(user)
    except Exception as e:
        logger.error(f"Failed to send registration email: {e}")
        # Continue with registration even if email fails

    try:
        db.add(user)
        db.commit()
    except Exception as e:
        logger.error(f"Database error during signup: {e}")
        db.rollback()
        raise HTTPException(
            status_code=500, detail="Failed to create account. Please try again."
        )


@auth_router.get("/user", status_code=status.HTTP_200_OK)
async def get_current_user(
    user: User = Depends(get_user), db: Session = Depends(get_session)
):
    """
    Get current user profile information
    """
    return {
        "id": user.id,
        "username": user.username,
        "email": user.email,
        "first_name": user.first_name,
        "last_name": user.last_name,
        "ipi_number": user.ipi_number,
        "writer_ipi": user.writer_ipi,
        "writer_name": user.writer_name,
        "publisher_ipi": user.publisher_ipi,
        "publisher_name": user.publisher_name,
        "avatar_url": user.avatar_url,
        "account_activated": user.account_activated,
    }


@auth_router.get("/user/registration-info", status_code=status.HTTP_200_OK)
async def get_registration_info(
    user: User = Depends(get_user), db: Session = Depends(get_session)
):
    """
    Get user's registration info for MLC audit matching
    """
    # Combine first_name and last_name into legal_name
    legal_name = None
    if user.first_name or user.last_name:
        parts = [p for p in [user.first_name, user.last_name] if p]
        legal_name = " ".join(parts) if parts else None

    return {
        "legal_name": legal_name,
        "writer_ipi": user.writer_ipi,
        "writer_name": user.writer_name,
        "publisher_name": user.publisher_name,
        "publisher_ipi": user.publisher_ipi,
    }


@auth_router.put("/user/registration-info", status_code=status.HTTP_200_OK)
async def update_registration_info(
    request: Request,
    user: User = Depends(get_user),
    db: Session = Depends(get_session),
):
    """
    Update user's registration info for MLC audit matching
    """
    try:
        body = await request.json()

        # Handle legal_name - split into first_name and last_name
        legal_name = body.get("legal_name")
        if legal_name is not None:
            if legal_name:
                parts = legal_name.strip().split(" ", 1)
                user.first_name = parts[0] if parts else None
                user.last_name = parts[1] if len(parts) > 1 else None
            else:
                user.first_name = None
                user.last_name = None

        # Update IPI fields
        if "writer_ipi" in body:
            user.writer_ipi = body.get("writer_ipi") or None
        if "writer_name" in body:
            user.writer_name = body.get("writer_name") or None
        if "publisher_name" in body:
            user.publisher_name = body.get("publisher_name") or None
        if "publisher_ipi" in body:
            user.publisher_ipi = body.get("publisher_ipi") or None

        db.commit()
        db.refresh(user)

        # Return updated values
        legal_name_response = None
        if user.first_name or user.last_name:
            parts = [p for p in [user.first_name, user.last_name] if p]
            legal_name_response = " ".join(parts) if parts else None

        return {
            "success": True,
            "legal_name": legal_name_response,
            "writer_ipi": user.writer_ipi,
            "writer_name": user.writer_name,
            "publisher_name": user.publisher_name,
            "publisher_ipi": user.publisher_ipi,
        }
    except Exception as e:
        logger.error(f"Error updating registration info: {e}")
        raise HTTPException(
            status_code=500, detail="Failed to update registration info"
        )


@auth_router.get("/user/subscription", status_code=status.HTTP_200_OK)
async def get_user_subscription(
    user: User = Depends(get_user), db: Session = Depends(get_session)
):
    """
    Get current user's subscription information including tier and usage
    """
    subscription = user.get_active_subscription()

    if not subscription:
        return {
            "tier": "Free",
            "scans_limit": 0,
            "scans_used": 0,
            "catalog_limit": 0,
            "catalog_used": 0,
            "auto_register_enabled": getattr(user, "auto_register_enabled", False) or False,
        }

    # Count total scans for the user
    from app.models.models import ACRCloudScan

    scans_used = db.query(ACRCloudScan).filter(ACRCloudScan.user_id == user.id).count()

    # Count total catalog entries
    from app.models.models import UserCatalog

    catalog_used = db.query(UserCatalog).filter(UserCatalog.user_id == user.id).count()

    # Define limits based on tier (from env config)
    tier_limits = {
        "Free": {"scans": 0, "catalog": 0},
        "Essential": {
            "scans": settings.essential_scans_limit,
            "catalog": settings.essential_catalog_limit,
        },
        "Pro": {
            "scans": settings.pro_scans_limit,
            "catalog": settings.pro_catalog_limit,
        },
        "Elite": {
            "scans": settings.elite_scans_limit,
            "catalog": settings.elite_catalog_limit,
        },
        "Enterprise": {
            "scans": settings.enterprise_scans_limit,
            "catalog": settings.enterprise_catalog_limit,
        },
    }

    tier_name = (
        subscription.tier.value
        if hasattr(subscription.tier, "value")
        else str(subscription.tier)
    )
    limits = tier_limits.get(tier_name, tier_limits["Free"])

    return {
        "tier": tier_name,
        "scans_limit": limits["scans"],
        "scans_used": scans_used,
        "catalog_limit": limits["catalog"],
        "catalog_used": catalog_used,
        "auto_register_enabled": getattr(user, "auto_register_enabled", False) or False,
    }


@auth_router.patch("/user", status_code=status.HTTP_200_OK)
async def update_user(
    update_data: UpdateUserRequest,
    user: User = Depends(get_user),
    db: Session = Depends(get_session),
):
    """
    Update user profile information (first name, last name, IPI numbers, publisher name)
    """
    try:
        # Update only the fields that are provided
        if update_data.username is not None:
            # Check uniqueness
            existing = db.query(User).filter(User.username == update_data.username, User.id != user.id).first()
            if existing:
                raise HTTPException(status_code=400, detail="This username is already taken.")
            user.username = update_data.username
        if update_data.first_name is not None:
            user.first_name = update_data.first_name
        if update_data.last_name is not None:
            user.last_name = update_data.last_name
        if update_data.ipi_number is not None:
            user.ipi_number = update_data.ipi_number
        if update_data.writer_ipi is not None:
            user.writer_ipi = update_data.writer_ipi
        if update_data.writer_name is not None:
            user.writer_name = update_data.writer_name
        if update_data.publisher_ipi is not None:
            user.publisher_ipi = update_data.publisher_ipi
        if update_data.publisher_name is not None:
            user.publisher_name = update_data.publisher_name

        db.commit()
        db.refresh(user)

        return {
            "id": user.id,
            "username": user.username,
            "email": user.email,
            "first_name": user.first_name,
            "last_name": user.last_name,
            "ipi_number": user.ipi_number,
            "writer_ipi": user.writer_ipi,
            "writer_name": user.writer_name,
            "publisher_ipi": user.publisher_ipi,
            "publisher_name": user.publisher_name,
            "avatar_url": user.avatar_url,
        }
    except Exception as e:
        logger.error(f"Error updating user profile: {e}")
        raise HTTPException(status_code=500, detail="Failed to update user profile")


@auth_router.post("/user/avatar", status_code=status.HTTP_200_OK)
async def upload_avatar(
    file: UploadFile = File(...),
    user: User = Depends(get_user),
    db: Session = Depends(get_session),
):
    """
    Upload user avatar image
    """
    # Security: Allowed file extensions whitelist
    ALLOWED_EXTENSIONS = {"jpg", "jpeg", "png", "gif", "webp"}
    # Security: Max file size (5MB)
    MAX_FILE_SIZE = 5 * 1024 * 1024
    # Security: Image magic bytes for validation
    IMAGE_SIGNATURES = {
        b"\xff\xd8\xff": "jpg",  # JPEG
        b"\x89PNG\r\n\x1a\n": "png",  # PNG
        b"GIF87a": "gif",  # GIF87a
        b"GIF89a": "gif",  # GIF89a
        b"RIFF": "webp",  # WebP (starts with RIFF)
    }

    try:
        # Validate content type
        if not file.content_type.startswith("image/"):
            raise HTTPException(status_code=400, detail="File must be an image")

        # Validate file extension
        if not file.filename or "." not in file.filename:
            raise HTTPException(status_code=400, detail="Invalid filename")
        file_extension = file.filename.rsplit(".", 1)[-1].lower()
        if file_extension not in ALLOWED_EXTENSIONS:
            raise HTTPException(
                status_code=400,
                detail=f"File type not allowed. Allowed: {', '.join(ALLOWED_EXTENSIONS)}",
            )

        # Read file content
        content = await file.read()

        # Validate file size
        if len(content) > MAX_FILE_SIZE:
            raise HTTPException(
                status_code=413, detail="File too large. Maximum size is 5MB"
            )

        # Validate magic bytes (actual file content)
        is_valid_image = False
        for signature in IMAGE_SIGNATURES:
            if content.startswith(signature):
                is_valid_image = True
                break
        if not is_valid_image:
            raise HTTPException(status_code=400, detail="Invalid image file content")

        # Get uploads directory path
        uploads_dir = os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
            "uploads",
            "avatars",
        )

        # Generate unique filename with validated extension
        filename = f"user_{user.id}_{int(time.time())}.{file_extension}"
        file_path = os.path.join(uploads_dir, filename)

        # Save file
        with open(file_path, "wb") as buffer:
            buffer.write(content)

        # Update user avatar_url - store just the filename
        user.avatar_url = filename
        db.commit()

        return {"avatar_url": filename}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error uploading avatar: {e}")
        raise HTTPException(status_code=500, detail="Failed to upload avatar")


@auth_router.get("/user/avatar", status_code=status.HTTP_200_OK)
async def get_user_avatar(user: User = Depends(get_user)):
    """
    Get the current user's avatar image
    Returns the avatar file if it exists, otherwise returns a 404
    """
    try:
        if not user.avatar_url:
            raise HTTPException(status_code=404, detail="No avatar found")

        # Construct the full path to the avatar
        uploads_dir = os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
            "uploads",
            "avatars",
        )
        avatar_path = os.path.join(uploads_dir, user.avatar_url)

        # Check if the file exists
        if not os.path.exists(avatar_path):
            logger.warning(f"Avatar file not found: {avatar_path}")
            raise HTTPException(status_code=404, detail="Avatar file not found")

        # Determine media type based on file extension
        file_extension = user.avatar_url.split(".")[-1].lower()
        media_type_map = {
            "jpg": "image/jpeg",
            "jpeg": "image/jpeg",
            "png": "image/png",
            "gif": "image/gif",
            "webp": "image/webp",
        }
        media_type = media_type_map.get(file_extension, "image/jpeg")

        return FileResponse(
            path=avatar_path,
            media_type=media_type,
            headers={"Cache-Control": "public, max-age=3600"},
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error retrieving avatar: {e}")
        raise HTTPException(status_code=500, detail="Failed to retrieve avatar")


@auth_router.delete("/user", status_code=status.HTTP_204_NO_CONTENT)
async def delete_user(
    token: str = Depends(oauth2_bearer), db: Session = Depends(get_session)
):
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        username: str = payload.get("sub")
        if username is None:
            raise HTTPException(status_code=403, detail="Token is invalid or expired")

        # user is legit, delete him with all the corresponding resources
        user = db.query(User).filter(User.username == username).first()

        if not user:
            raise HTTPException(status_code=404, detail="User not found")

        # delete stripe customer - continue even if this fails
        # (customer may not exist, or test/live mode mismatch)
        if user.stripe_customer_id:
            try:
                stripe.Customer.delete(user.stripe_customer_id)
            except stripe.error.InvalidRequestError as e:
                logger.warning(
                    f"Could not delete Stripe customer '{user.stripe_customer_id}' "
                    f"for user '{username}': {e}. Continuing with user deletion."
                )
            except Exception as e:
                logger.warning(
                    f"Unexpected error deleting Stripe customer '{user.stripe_customer_id}' "
                    f"for user '{username}': {e}. Continuing with user deletion."
                )

        # delete all dependent records before deleting the user
        db.query(Notification).filter(Notification.user_id == user.id).delete()
        db.query(PushSubscription).filter(PushSubscription.user_id == user.id).delete()
        db.query(DeviceToken).filter(DeviceToken.user_id == user.id).delete()
        db.query(Agreement).filter(Agreement.user_id == user.id).delete()
        db.query(PublishingAgreement).filter(
            PublishingAgreement.user_id == user.id
        ).delete()
        db.query(RevenueTransaction).filter(
            RevenueTransaction.user_id == user.id
        ).delete()
        db.query(RevenueStatement).filter(RevenueStatement.user_id == user.id).delete()
        db.query(ACRCloudScan).filter(ACRCloudScan.user_id == user.id).delete()
        db.query(BatchUpload).filter(BatchUpload.user_id == user.id).delete()
        db.query(Subscription).filter(Subscription.user_id == user.id).delete()
        db.query(Client).filter(Client.user_id == user.id).delete()

        # delete user (UserCatalog cascades via model config)
        db.delete(user)
        db.commit()

    except JWTError:
        raise HTTPException(status_code=403, detail="Token is invalid or expired")
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Could not delete user: {e}")
        db.rollback()
        raise HTTPException(status_code=500, detail="Failed to delete user account.")


@auth_router.get("/confirm-user/{token}", status_code=status.HTTP_200_OK)
async def confirm_user(token: str, db: Session = Depends(get_session)):
    try:
        email = email_client.serializer.loads(
            token, salt="email-confirmation", max_age=3600
        )
    except (BadSignature, SignatureExpired) as e:
        logger.warning(f"Email confirmation token invalid or expired: {e}")
        return RedirectResponse(
            url=urljoin(settings.base_url_frontend, "/email-invalid")
        )

    user = db.query(User).filter(User.email == email).first()
    if not user:
        return RedirectResponse(
            url=urljoin(settings.base_url_frontend, "/email-invalid")
        )

    user.account_activated = True
    db.commit()

    # Send confirmation email but don't fail if it doesn't work
    try:
        email_client.send_confirmed_email(user)
    except Exception as e:
        logger.error(f"Failed to send confirmation email to {user.email}: {e}")
        # Continue with redirect even if email fails

    return RedirectResponse(url=urljoin(settings.base_url_frontend, "/email-confirmed"))


@auth_router.post("/login", response_model=Token, status_code=status.HTTP_200_OK)
@auth_router.post("/token", response_model=Token, status_code=status.HTTP_200_OK)
async def login_user(
    request: Request,
    form_data=Depends(OAuth2PasswordRequestForm),
    db: Session = Depends(get_session),
):
    logger.info(f"Login attempt for user: {form_data.username}")
    # Rate limiting
    await check_rate_limit(request, login_rate_limiter)

    # Check if account is locked
    account_lockout.check_lockout(form_data.username)

    # Support login with username OR email
    user = (
        db.query(User)
        .filter(
            (User.username == form_data.username) | (User.email == form_data.username)
        )
        .first()
    )
    if not user:
        # Record failed attempt even if user doesn't exist (prevent user enumeration)
        account_lockout.record_failed_attempt(form_data.username)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="User does not exist."
        )

    if not bcrypt_context.verify(form_data.password, user.hashed_password):
        # Record failed login attempt
        account_lockout.record_failed_attempt(form_data.username)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User does not exist.",  # do not reveal existence of user
        )

    # Successful login - clear any failed attempts
    account_lockout.record_successful_login(form_data.username)

    # user found, authenticate
    encode = {"sub": user.username, "id": user.id, "email": user.email}
    expires = datetime.now() + timedelta(days=7)
    encode.update({"exp": expires})
    return {
        "access_token": jwt.encode(encode, key=SECRET_KEY, algorithm=ALGORITHM),
        "token_type": "bearer",
    }


@auth_router.post("/resend-verification", status_code=status.HTTP_200_OK)
async def resend_verification_email(
    user: User = Depends(get_user),
    db: Session = Depends(get_session),
):
    """
    Resend the email verification link to the user's email address.
    """
    if user.account_activated:
        raise HTTPException(status_code=400, detail="Account is already verified.")

    try:
        email_client.send_register_email(user)
        return {"message": "Verification email sent successfully."}
    except Exception as e:
        logger.error(f"Failed to resend verification email to {user.email}: {e}")
        raise HTTPException(
            status_code=500,
            detail="Failed to send verification email. Please try again later.",
        )


@auth_router.post("/resend-verification-by-email", status_code=status.HTTP_200_OK)
async def resend_verification_by_email(
    request_body: ResetPasswordEmail,
    db: Session = Depends(get_session),
):
    """
    Resend the email verification link using just an email address (no auth required).
    Used when users land on the email-invalid page with an expired token.
    """
    # Always return success to prevent email enumeration
    user = db.query(User).filter(User.email == request_body.email).first()

    if user and not user.account_activated:
        try:
            email_client.send_register_email(user)
        except Exception as e:
            logger.error(
                f"Failed to resend verification email to {request_body.email}: {e}"
            )

    return {
        "message": "If an account exists with this email and is not yet verified, a verification email has been sent."
    }


@auth_router.post("/change-password", status_code=status.HTTP_200_OK)
async def change_password(
    password_data: NewPassword,
    user: User = Depends(get_user),
    db: Session = Depends(get_session),
):
    # authenticated user, but still verify with old password
    if not bcrypt_context.verify(password_data.password_old, user.hashed_password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="The old password does not match.",  # do not reveal existence of user
        )
    user.hashed_password = bcrypt_context.hash(password_data.password_new)
    db.commit()


@auth_router.post("/reset-password", status_code=status.HTTP_200_OK)
async def reset_password(
    password_data: ResetPassword, db: Session = Depends(get_session)
):
    # reset via email
    if password_data.password_new != password_data.password_new_retyped:
        raise HTTPException(status_code=400, detail="Passwords do not match.")
    user_email = auth_s.loads(password_data.token)
    user = db.query(User).filter(User.email == user_email).first()
    if not user:
        raise HTTPException(status_code=400, detail="User not found.")

    user.hashed_password = bcrypt_context.hash(password_data.password_new)
    db.commit()


@auth_router.post("/send-reset-password-email", status_code=status.HTTP_200_OK)
async def send_reset_password_email(
    request_body: ResetPasswordEmail,
    request: Request,
    db: Session = Depends(get_session),
):
    try:
        await check_rate_limit(request, password_reset_limiter)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Rate limit check failed: {e}")

    try:
        user = db.query(User).filter(User.email == request_body.email).first()
        if user:
            logger.info(f"Sending reset password email to {request_body.email}")
            email_client.send_reset_password_email(user)
            logger.info(f"Reset password email sent successfully to {request_body.email}")
        else:
            logger.info(f"No user found for email {request_body.email}")
    except Exception as e:
        import traceback
        logger.error(
            f"Failed to send reset password email to {request_body.email}: {e}\n{traceback.format_exc()}"
        )
    # Always return 200 - don't reveal if email exists
    return {"status": "ok"}


@auth_router.post(
    "/create-google-user/{google_token}", status_code=status.HTTP_201_CREATED
)
async def create_google_user(google_token: str, db: Session = Depends(get_session)):
    response = requests.get(
        "https://www.googleapis.com/oauth2/v1/userinfo",
        headers={"Authorization": f"Bearer {google_token}"},
    )
    if not response.ok:
        raise HTTPException(
            status_code=400, detail="Failed to verify Google account. Please try again."
        )
    body = response.json()
    if "email" not in body or "name" not in body:
        raise HTTPException(
            status_code=400, detail="Could not retrieve account info from Google."
        )
    name = body["name"].replace(" ", "_")
    email = body["email"]
    email_verified = body.get("verified_email", False)

    # allow only verified emails
    if not email_verified:
        raise HTTPException(
            status_code=400, detail="Please verify your email before registering."
        )

    # check first if the user already exists
    existing_user = db.query(User).filter(User.email == email).first()

    if existing_user:
        raise HTTPException(status_code=400, detail="The user is already registered.")

    # create stripe customer
    try:
        customer = stripe.Customer.create(
            name=name,
            email=email,
            description=name,
            test_clock=(
                stripe.test_helpers.TestClock.create(frozen_time=int(time.time()))
                if os.environ.get("env") == "development"
                else None
            ),
        )

        user = User(
            username=name,
            email=email,
            stripe_customer_id=customer["id"],
            activated=False,
            royalty_per_stream=royalty_dict["Worldwide"],
        )
        db.add(user)
        db.commit()

        # Send confirmation email
        try:
            email_client.send_register_email(user)
        except Exception as e:
            logger.error(f"Failed to send registration email for Google user: {e}")
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Google signup error: {type(e).__name__}: {e}")
        raise HTTPException(
            status_code=500,
            detail=f"Signup failed: {type(e).__name__}: {str(e)}",
        )


@auth_router.post(
    "/google-login/{google_token}", response_model=Token, status_code=status.HTTP_200_OK
)
async def login_google_user(google_token: str, db: Session = Depends(get_session)):
    response = requests.get(
        "https://www.googleapis.com/oauth2/v1/userinfo",
        headers={"Authorization": f"Bearer {google_token}"},
    )
    if not response.ok:
        raise HTTPException(
            status_code=400, detail="Failed to verify Google account. Please try again."
        )
    body = response.json()
    if "email" not in body:
        raise HTTPException(
            status_code=400, detail="Could not retrieve email from Google."
        )
    email = body["email"]

    # try to fetch user from database
    user = db.query(User).filter(User.email == email).first()
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="User does not exist."
        )

    # user found, authenticate
    encode = {"sub": user.username, "id": user.id, "email": user.email}
    expires = datetime.now() + timedelta(days=7)
    encode.update({"exp": expires})
    return {
        "access_token": jwt.encode(encode, key=SECRET_KEY, algorithm=ALGORITHM),
        "token_type": "bearer",
    }


# call this when opening a protected route
def _decode_session_token(token: str) -> dict:
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
    except JWTError:
        raise HTTPException(status_code=403, detail="Token is invalid or expired")
    if payload.get("sub") is None:
        raise HTTPException(status_code=403, detail="Token is invalid or expired")
    return payload


@auth_router.get("/verify-token")
async def verify_token_header(request: Request):
    """Validate the caller's session, taking the token from the Authorization
    header.

    The token must NOT travel in the URL: request lines are written verbatim to
    the access log, so the path-based variant below put live session tokens —
    including an admin's — in plaintext on disk, where anyone with log access
    could replay them until expiry. Headers are not logged.
    """
    header = request.headers.get("authorization") or ""
    if not header.lower().startswith("bearer "):
        raise HTTPException(status_code=403, detail="Token is invalid or expired")
    return _decode_session_token(header[7:].strip())


@auth_router.get("/verify-token/{token}", deprecated=True)
async def verify_token(token: str):
    """DEPRECATED — use GET /auth/verify-token with an Authorization header.

    Kept only so an older client build keeps working through a deploy; it leaks
    the token into access logs by construction. Remove once no client calls it.
    """
    return _decode_session_token(token)


# helper functions


def uses_service(
    hashed_password: Union[str, Literal["google", "apple", "facebook", "twitter"]],
):
    return hashed_password in ["google", "apple", "facebook", "twitter"]


def validate_captcha(token):
    try:
        api_key = settings.google_api_key
        site_key = settings.recaptcha_site_key
        api_link = f"https://recaptchaenterprise.googleapis.com/v1/projects/tunescan-ba746/assessments?key={api_key}"

        response = requests.post(
            url=api_link,
            json={
                "event": {
                    "token": token,
                    "expectedAction": "LOGIN",
                    "siteKey": site_key,
                }
            },
        )
        response_json = response.json()

        if "riskAnalysis" not in response_json:
            print(
                f"[ERROR] reCAPTCHA validation failed - no riskAnalysis in response: {response_json}"
            )
            return False

        user_score = response_json["riskAnalysis"]["score"]
        print(f"[DEBUG] reCAPTCHA score: {user_score}")
        return user_score > 0.7
    except Exception as e:
        print(f"[ERROR] reCAPTCHA validation exception: {str(e)}")
        return False


@auth_router.post("/auth")
async def authenticate_user(
    request: AuthRequest,
    user: User = Depends(get_user),
    db: Session = Depends(get_session),
):
    # the spotify me endpoints only work on users who are not web apps

    if request.authority == "spotify":
        # get user data and save the spotify user id
        try:
            response = requests.get(
                url="https://api.spotify.com/v1/me",
                headers={"Authorization": f"Bearer {request.token}"},
            )
            if not response.ok:
                raise HTTPException(status_code=response.status_code, detail=response)
            body = response.json()
            user.spotify_user_id = body["id"]
            user.genius_user_id = None
            db.commit()
        except RequestException as e:
            raise HTTPException(status_code=500, detail=e)
    elif request.authority == "genius":
        # get user data and save the spotify user id
        try:
            response = requests.get(
                url="https://api.genius.com/account",
                headers={"Authorization": f"Bearer {request.token}"},
            )
            body = response.json()
            user.genius_user_id = body["response"]["user"]["id"]
            user.spotify_user_id = None
            db.commit()
        except RequestException as e:
            raise HTTPException(status_code=500, detail=e)


@auth_router.post("/user/publishing-agreement", status_code=status.HTTP_201_CREATED)
async def submit_publishing_agreement(
    request: Request,
    user: User = Depends(get_user),
    db: Session = Depends(get_session),
):
    """
    Record a user's publishing agreement submission for royalty collection.
    This is called when a user agrees to let Verax collect their unclaimed royalties.
    """
    try:
        body = await request.json()
        agreement_type = body.get("agreement_type", "")
        songs = body.get("songs", [])

        logger.info(
            f"Publishing agreement submitted by user {user.id}: "
            f"type={agreement_type}, songs={len(songs)}"
        )

        # Store the agreement in the database
        from app.models.models import PublishingAgreement

        agreement = PublishingAgreement(
            user_id=user.id,
            agreement_type=agreement_type,
            song_count=len(songs),
            songs_data=songs,
        )
        db.add(agreement)
        db.commit()

        return {"success": True, "agreement_id": agreement.id}
    except Exception as e:
        logger.error(f"Error submitting publishing agreement: {e}")
        raise HTTPException(
            status_code=500, detail="Failed to submit publishing agreement"
        )
