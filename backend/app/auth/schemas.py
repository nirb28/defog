"""Request and response shapes for accounts and sessions."""

import uuid
from datetime import datetime
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator

from ..models import User

Year = Literal["Freshman", "Sophomore", "Junior", "Senior", "Graduate", "Other"]
Client = Literal["web", "mobile"]


def _check_time_zone(value: str) -> str:
    value = value.strip()
    try:
        ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError("Unknown time zone. Use a name like America/New_York.") from exc
    return value


def _clean(value: str | None) -> str | None:
    if value is None:
        return None
    value = " ".join(value.split())
    return value or None


class RegisterRequest(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    email: EmailStr
    password: str = Field(min_length=8, max_length=256)
    school: str = Field(min_length=1, max_length=200)
    year: Year | None = None
    major: str | None = Field(default=None, max_length=200)
    time_zone: str
    accept_terms: bool
    client: Client = "web"

    _clean_text = field_validator("name", "school", "major")(_clean)
    _tz = field_validator("time_zone")(_check_time_zone)

    @field_validator("name", "school")
    @classmethod
    def _required(cls, value: str | None) -> str:
        if not value:
            raise ValueError("This field is required.")
        return value

    @field_validator("accept_terms")
    @classmethod
    def _must_accept(cls, value: bool) -> bool:
        if value is not True:
            raise ValueError("You need to accept the terms and privacy policy.")
        return value


class LoginRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=256)
    client: Client = "web"


class TokenRequest(BaseModel):
    token: str = Field(min_length=10, max_length=200)


class ResetPasswordRequest(TokenRequest):
    password: str = Field(min_length=8, max_length=256)


class ForgotPasswordRequest(BaseModel):
    email: EmailStr


class RefreshRequest(BaseModel):
    refresh_token: str = Field(min_length=10, max_length=200)


class LinkGoogleRequest(TokenRequest):
    password: str = Field(min_length=1, max_length=256)
    client: Client = "web"


class ProfileUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    school: str | None = Field(default=None, min_length=1, max_length=200)
    year: Year | None = None
    major: str | None = Field(default=None, max_length=200)
    time_zone: str | None = None
    accept_terms: bool | None = None

    _clean_text = field_validator("name", "school", "major")(_clean)

    @field_validator("time_zone")
    @classmethod
    def _tz(cls, value: str | None) -> str | None:
        return None if value is None else _check_time_zone(value)


class DeleteAccountRequest(BaseModel):
    # Typing the account's email confirms intent and works for Google-only accounts too.
    confirm_email: EmailStr


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    email: str
    email_verified: bool
    school: str | None
    year: str | None
    major: str | None
    time_zone: str | None
    consent_at: datetime | None
    profile_complete: bool
    has_password: bool
    google_linked: bool
    created_at: datetime

    @classmethod
    def of(cls, user: User) -> "UserOut":
        return cls(
            id=user.id,
            name=user.name,
            email=user.email,
            email_verified=user.email_verified,
            school=user.school,
            year=user.year,
            major=user.major,
            time_zone=user.time_zone,
            consent_at=user.consent_at,
            profile_complete=user.profile_complete,
            has_password=user.password_hash is not None,
            google_linked=user.google_sub is not None,
            created_at=user.created_at,
        )


class AuthResponse(BaseModel):
    """Web clients get csrf_token (the session itself is in a cookie); mobile gets tokens."""

    user: UserOut
    csrf_token: str | None = None
    access_token: str | None = None
    refresh_token: str | None = None
    token_type: str | None = None
    expires_in: int | None = None


class AuthOptions(BaseModel):
    google_enabled: bool
    password_min_length: int = 8
