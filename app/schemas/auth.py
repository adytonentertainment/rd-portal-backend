from enum import Enum

from app.utils.password_validator import validate_password
from pydantic import BaseModel, EmailStr, field_validator


# auth
class Token(BaseModel):
    access_token: str
    token_type: str


class NewPassword(BaseModel):
    password_old: str
    password_new: str

    @field_validator("password_new")
    @classmethod
    def validate_password_strength(cls, v: str) -> str:
        is_valid, errors = validate_password(v)
        if not is_valid:
            raise ValueError("; ".join(errors))
        return v


class ResetPassword(BaseModel):
    password_new: str
    password_new_retyped: str
    token: str

    @field_validator("password_new")
    @classmethod
    def validate_password_strength(cls, v: str) -> str:
        is_valid, errors = validate_password(v)
        if not is_valid:
            raise ValueError("; ".join(errors))
        return v


class ResetPasswordEmail(BaseModel):
    email: EmailStr


class Authority(str, Enum):
    spotify = "spotify"
    genius = "genius"


class AuthRequest(BaseModel):
    token: str
    authority: Authority


class AuthorityResponse(BaseModel):
    user_id: str
    authority: Authority
