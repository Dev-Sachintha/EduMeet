"""Password hashing, JWT, and two-step verification (TOTP) helpers."""
import base64
import hashlib
import hmac
import io
import re
import secrets
import time
from datetime import datetime, timedelta, timezone

import bcrypt
import jwt
import pyotp
import segno

from backend.config import settings

ALGORITHM = "HS256"


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8")[:72], bcrypt.gensalt()).decode("utf-8")


def verify_password(password: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode("utf-8")[:72], hashed.encode("utf-8"))
    except ValueError:
        return False


def create_access_token(user_id: int, role: str, session_id: str) -> str:
    expire = datetime.now(timezone.utc) + timedelta(minutes=settings.access_token_expire_minutes)
    return jwt.encode(
        {"sub": str(user_id), "role": role, "sid": session_id, "exp": expire},
        settings.secret_key,
        algorithm=ALGORITHM,
    )


def decode_access_token(token: str) -> dict | None:
    try:
        return jwt.decode(token, settings.secret_key, algorithms=[ALGORITHM])
    except jwt.PyJWTError:
        return None


# ----------------------------- authenticator app (TOTP) -----------------------------
def new_totp_secret() -> str:
    return pyotp.random_base32()


def totp_uri(secret: str, email: str) -> str:
    return pyotp.TOTP(secret).provisioning_uri(name=email, issuer_name="EduMeet")


def verify_totp(secret: str, code: str, last_step: int) -> int | None:
    """Return the matched 30-second step, or None. A step can be used only once (no replay)."""
    code = (code or "").strip().replace(" ", "")
    if not (code.isdigit() and len(code) == 6):
        return None
    totp, now = pyotp.TOTP(secret), int(time.time())
    for offset in (0, -1, 1):  # tolerate one step of clock drift
        step = now // 30 + offset
        if step > last_step and hmac.compare_digest(totp.at(step * 30), code):
            return step
    return None


def qr_png_base64(text: str) -> str:
    buffer = io.BytesIO()
    segno.make(text, error="m").save(buffer, kind="png", scale=6, border=2)
    return base64.b64encode(buffer.getvalue()).decode("ascii")


# ----------------------------- recovery codes -----------------------------
def generate_recovery_codes(count: int = 8) -> list[str]:
    codes: set[str] = set()
    while len(codes) < count:
        h = secrets.token_hex(4)
        codes.add(f"{h[:4]}-{h[4:]}")
    return sorted(codes)


def normalize_recovery(code: str) -> str | None:
    """'A1B2 c3d4' -> 'a1b2-c3d4'; anything that is not 8 hex characters -> None."""
    h = re.sub(r"[^0-9a-f]", "", (code or "").lower())
    return f"{h[:4]}-{h[4:]}" if len(h) == 8 else None


def hash_recovery(code: str) -> str:
    return hashlib.sha256((settings.secret_key + code).encode("utf-8")).hexdigest()


def hash_email_code(code: str) -> str:
    return hashlib.sha256((settings.secret_key + "email:" + code).encode("utf-8")).hexdigest()
