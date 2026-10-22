"""Pydantic request/response schemas."""
from datetime import datetime

from pydantic import BaseModel, ConfigDict, EmailStr, Field

from backend.models import Role


class LoginRequest(BaseModel):
    email: str
    password: str
    otp: str = Field(default="", max_length=20)   # authenticator code or recovery code
    device_id: str = Field(default="", max_length=64)


class RegisterRequest(BaseModel):
    email: EmailStr
    full_name: str = Field(min_length=2, max_length=120)
    password: str = Field(min_length=8, max_length=72)  # bcrypt limit is 72 bytes
    role: Role = Role.student
    device_id: str = Field(default="", max_length=64)


class UserUpdate(BaseModel):
    full_name: str | None = Field(default=None, min_length=2, max_length=120)
    role: Role | None = None
    is_active: bool | None = None
    password: str | None = Field(default=None, min_length=8, max_length=72)
    reset_2fa: bool = False


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    email: str
    full_name: str
    role: str
    is_active: bool
    created_at: datetime
    totp_enabled: bool = False


class TokenResponse(BaseModel):
    access_token: str | None = None
    token_type: str = "bearer"
    user: UserOut | None = None
    otp_required: bool = False   # password was right, now ask for the authenticator code
    verification_required: bool = False  # e-mail not confirmed yet: a code was sent to the address


class MeetingCreate(BaseModel):
    title: str = Field(min_length=3, max_length=200)
    description: str = Field(default="", max_length=2000)
    scheduled_at: datetime | None = None


class MeetingOut(BaseModel):
    id: int
    code: str
    title: str
    description: str
    host_id: int
    host_name: str
    status: str
    scheduled_at: datetime | None
    created_at: datetime
    ended_at: datetime | None


class FileOut(BaseModel):
    id: int
    meeting_id: int
    original_name: str
    size: int
    content_type: str
    uploader_name: str
    created_at: datetime


class TwoFactorSetup(BaseModel):
    secret: str
    otpauth_uri: str
    qr_png_base64: str


class CodeRequest(BaseModel):
    code: str = Field(min_length=6, max_length=20)


class PasswordCodeRequest(BaseModel):
    password: str = Field(max_length=72)
    code: str = Field(min_length=6, max_length=20)


class RecoveryCodesOut(BaseModel):
    codes: list[str]


class PasswordResetRequest(BaseModel):
    email: str
    code: str = Field(min_length=6, max_length=20)
    new_password: str = Field(min_length=8, max_length=72)


class VerifyEmailRequest(BaseModel):
    email: str
    code: str = Field(min_length=6, max_length=6)
    device_id: str = Field(default="", max_length=64)


class ResendRequest(BaseModel):
    email: str
