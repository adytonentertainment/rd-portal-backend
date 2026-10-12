import os
import time
from datetime import datetime, timedelta
from urllib.parse import urljoin

from app.database.session import get_session
from app.emails import get_email_client
from app.logger import get_logger
from app.middleware.account_lockout import account_lockout
from app.services import throttle as throttle_svc
from app.middleware.rate_limit import (
    check_rate_limit,
    login_rate_limiter,
    password_reset_limiter,
)
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
    NewPassword,
    ResetPassword,
    ResetPasswordEmail,
    Token,
)
from app.schemas.user import UpdateUserRequest
from app.settings.settings import get_settings
from app.utils.password_validator import MIN_LENGTH, get_password_requirements
from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from jose import JWTError, jwt
from passlib.context import CryptContext
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
    # Durable lockout. The in-memory check below still runs, but it is only a
    # process-local cache now: it was erased by every deploy, so on its own it
    # let an attacker simply wait for the next restart.
    _locked = throttle_svc.locked_until(db, "lockout", form_data.username.lower())
    if _locked:
        mins = max(1, int((_locked - datetime.now()).total_seconds() // 60))
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"Too many failed attempts. Try again in {mins} minute(s).",
        )
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
        throttle_svc.record_failure(
            db, "lockout", form_data.username.lower(),
            max_attempts=5, window=timedelta(minutes=15), lockout=timedelta(minutes=30),
        )
        account_lockout.record_failed_attempt(form_data.username)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="User does not exist."
        )

    if not bcrypt_context.verify(form_data.password, user.hashed_password):
        # Record failed login attempt
        throttle_svc.record_failure(
            db, "lockout", form_data.username.lower(),
            max_attempts=5, window=timedelta(minutes=15), lockout=timedelta(minutes=30),
        )
        account_lockout.record_failed_attempt(form_data.username)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User does not exist.",  # do not reveal existence of user
        )

    # Successful login - clear any failed attempts
    throttle_svc.clear(db, "lockout", form_data.username.lower())
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
