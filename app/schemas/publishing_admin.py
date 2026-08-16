from typing import Optional, Literal
from pydantic import BaseModel, EmailStr


class PublishingAdminInitiateRequest(BaseModel):
    legalName: str
    producerName: str
    email: EmailStr
    address: str
    city: str
    state: str
    zip: str
    country: str = "United States"
    termType: Literal["3month", "2year"] = "3month"


class PublishingAdminInitiateResponse(BaseModel):
    agreement_id: int
    signing_url: str
    envelope_id: str


class PublishingAdminStatusResponse(BaseModel):
    envelope_id: str
    status: str
    signer_name: str
    created_at: str
    signed_at: Optional[str] = None
