import re
from typing import Literal, Optional, Union

from app.utils.password_validator import get_password_requirements, validate_password
from pydantic import BaseModel, EmailStr, field_validator


class UserBase(BaseModel):
    email: str


class CreateUserRequest(UserBase):
    username: str
    email: EmailStr
    password: str
    captchaToken: str

    @field_validator("username")
    @classmethod
    def validate_username(cls, v):
        if not v or len(v) < 3:
            raise ValueError("Username must be at least 3 characters")
        if len(v) > 30:
            raise ValueError("Username must be less than 30 characters")
        if not re.match(r"^[a-zA-Z0-9_.-]+$", v):
            raise ValueError(
                "Username can only contain letters, numbers, underscores, dots, and hyphens"
            )
        return v

    @field_validator("password")
    @classmethod
    def validate_password_strength(cls, v: str) -> str:
        is_valid, errors = validate_password(v)
        if not is_valid:
            raise ValueError("; ".join(errors))
        return v


class LoginForm(BaseModel):
    username: str
    password: str


class Subscription(BaseModel):
    id: int
    user_id: int
    subscription_id: str
    scans: int


class UpdateUserRequest(BaseModel):
    username: Optional[str] = None
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    ipi_number: Optional[str] = None  # Legacy field
    writer_ipi: Optional[str] = None
    writer_name: Optional[str] = None  # Writer/songwriter name
    publisher_ipi: Optional[str] = None
    publisher_name: Optional[str] = None


class User(UserBase):
    id: int
    username: str
    # user_repr: str
    email: str
    password: Union[str, Literal["google", "apple", "facebook", "twitter"]]
    acrcloud_container_id: int
    stripe_customer_id: str

    subscription: Subscription

    # subscription object
    # account_activated: bool
    # payment_method: str
    # payment_fingerprint: str

    class Config:
        # renamed from orm_mode
        from_attributes = True
