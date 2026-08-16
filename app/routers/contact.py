from fastapi import APIRouter, Depends, HTTPException
from app.routers.auth import get_user
from app.models.models import User
from app.schemas.contact import ClaimRequest
from app.settings.settings import get_settings
from app.emails import get_email_client

settings = get_settings()
contact_router = APIRouter(prefix="/contact", tags=["Contact"])
email_client = get_email_client()


@contact_router.post("/claim")
async def report_claim(request: ClaimRequest, user: User = Depends(get_user)):
    # Get the active subscription for the current stripe mode
    subscription = user.get_active_subscription()

    if not subscription:
        raise HTTPException(status_code=400, detail="You are not subscribed.")

    email_client.send_claim_email(user, request.artist, request.title, request.message)


@contact_router.post("/contact")
async def report_contact(user: User = Depends(get_user)):
    raise NotImplementedError()
