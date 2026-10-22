"""Authentication, sessions, e-mail rules, two-step verification and admin user management."""
import hmac
import secrets
import uuid
from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import delete, func, select, update
from sqlalchemy.orm import Session

from backend.audit import log_event
from backend.config import settings
from backend.deps import get_current_auth, get_current_user, get_db, require_admin
from backend.emailcheck import EmailRejected, canonical_email, check_new_email
from backend.mailer import send_mail
from backend.models import (
    SELF_REGISTER_ROLES, EmailCode, RecoveryCode, Role, User, UserSession, as_utc, utcnow,
)
from backend.schemas import (
    CodeRequest, LoginRequest, PasswordCodeRequest, PasswordResetRequest, RecoveryCodesOut,
    RegisterRequest, ResendRequest, TokenResponse, TwoFactorSetup, UserOut, UserUpdate, VerifyEmailRequest,
)
from backend.security import (
    create_access_token, generate_recovery_codes, hash_email_code, hash_password, hash_recovery,
    new_totp_secret, normalize_recovery, qr_png_base64, totp_uri, verify_password, verify_totp,
)

router = APIRouter(prefix="/api/auth", tags=["auth"])
admin_router = APIRouter(prefix="/api/users", tags=["users (admin)"])


# ----------------------------- helpers -----------------------------
def _ip(request: Request) -> str:
    return request.client.host if request.client else ""


def _mask(email: str) -> str:
    name, _, domain = email.partition("@")
    return f"{name[:1]}***@{domain}"


def _wait_text() -> str:
    return f"about {max(1, settings.session_idle_seconds // 60)} minute(s)"


def _live(session: UserSession) -> bool:
    return as_utc(session.last_seen) > utcnow() - timedelta(seconds=settings.session_idle_seconds)


def _is_locked(user: User) -> bool:
    return bool(user.locked_until and as_utc(user.locked_until) > utcnow())


def _check_lock(user: User) -> None:
    if _is_locked(user):
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            f"Too many failed attempts. Try again in {settings.lockout_minutes} minutes.",
        )


def _fail(db: Session, user: User, ip: str = "", kind: str = "login_fail", detail: str = "") -> None:
    """Count a failed attempt (committed immediately), log it, and lock the account after too many."""
    user.failed_attempts = (user.failed_attempts or 0) + 1
    locked = user.failed_attempts >= settings.max_failed_attempts
    if locked:
        user.locked_until = utcnow() + timedelta(minutes=settings.lockout_minutes)
        user.failed_attempts = 0
    db.commit()
    log_event(kind, user=user, ip=ip, detail=detail)
    if locked:
        log_event("account_locked", user=user, ip=ip, detail=f"{settings.lockout_minutes} minutes")


def _verify_second_factor(db: Session, user: User, code: str) -> bool:
    """Authenticator code (6 digits, single use) or an unused recovery code. Caller commits."""
    code = (code or "").strip()
    if not code or not user.totp_secret:
        return False
    step = verify_totp(user.totp_secret, code, user.last_totp_step or 0)
    if step is not None:
        user.last_totp_step = step
        return True
    normalized = normalize_recovery(code)
    if normalized:
        row = db.scalar(
            select(RecoveryCode).where(
                RecoveryCode.user_id == user.id,
                RecoveryCode.code_hash == hash_recovery(normalized),
                RecoveryCode.used_at.is_(None),
            )
        )
        if row:
            row.used_at = utcnow()
            return True
    return False


def _new_recovery_codes(db: Session, user: User) -> list[str]:
    db.execute(delete(RecoveryCode).where(RecoveryCode.user_id == user.id))
    codes = generate_recovery_codes(8)
    for code in codes:
        db.add(RecoveryCode(user_id=user.id, code_hash=hash_recovery(code)))
    return codes


def _revoke_sessions(db: Session, user_id: int) -> None:
    db.execute(
        update(UserSession)
        .where(UserSession.user_id == user_id, UserSession.revoked.is_(False))
        .values(revoked=True)
    )


def _ensure_device_free(db: Session, device_id: str, user_id: int | None) -> list[UserSession]:
    """One computer = one account at a time. Returns stale sessions of other accounts to revoke."""
    if not (settings.one_account_per_device and device_id):
        return []
    rows = db.scalars(
        select(UserSession).where(
            UserSession.device_id == device_id,
            UserSession.revoked.is_(False),
            UserSession.user_id != (user_id or 0),
        )
    ).all()
    for row in rows:
        if _live(row):
            owner = db.get(User, row.user_id)
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                f"Another account ({_mask(owner.email) if owner else 'unknown'}) is signed in on this computer. "
                f"Sign out of it first, or wait {_wait_text()} after closing the app.",
            )
    return list(rows)


def start_session(db: Session, user: User, device_id: str) -> str:
    """Apply the sign-in rules, then create a session. Raises 409 when a rule is broken."""
    device_id = (device_id or "").strip()[:64]
    stale_other_accounts = _ensure_device_free(db, device_id, user.id)
    mine = db.scalars(
        select(UserSession).where(UserSession.user_id == user.id, UserSession.revoked.is_(False))
    ).all()
    if settings.single_session_per_account:
        for row in mine:
            if _live(row) and (not device_id or row.device_id != device_id):
                raise HTTPException(
                    status.HTTP_409_CONFLICT,
                    "This account is already signed in on another device. Sign out there first, "
                    f"or wait {_wait_text()} after closing the app.",
                )
    for row in stale_other_accounts:
        row.revoked = True
    for row in mine:
        if settings.single_session_per_account or (device_id and row.device_id == device_id):
            row.revoked = True
    session = UserSession(id=uuid.uuid4().hex, user_id=user.id, device_id=device_id)
    db.add(session)
    db.commit()
    return session.id


def _token_response(user: User, session_id: str) -> TokenResponse:
    return TokenResponse(
        access_token=create_access_token(user.id, user.role, session_id), user=UserOut.model_validate(user)
    )


def _finish_login(db: Session, user: User, device_id: str, ip: str) -> TokenResponse:
    user.failed_attempts, user.locked_until = 0, None
    user.last_login_at, user.last_login_ip = utcnow(), ip or None
    try:
        session_id = start_session(db, user, device_id)  # commits; a 409 leaves everything (even an OTP) unused
    except HTTPException as exc:
        log_event("login_blocked", user=user, ip=ip, device_id=device_id, detail=str(exc.detail))
        raise
    log_event("login_ok", user=user, ip=ip, device_id=device_id)
    return _token_response(user, session_id)


def _email_taken(db: Session, email: str) -> bool:
    return db.scalar(select(User.id).where(User.email == email)) is not None


# ----------------------------- e-mail verification -----------------------------
def _send_verification(db: Session, user: User, *, force: bool) -> None:
    last = db.scalar(
        select(EmailCode).where(EmailCode.user_id == user.id).order_by(EmailCode.created_at.desc())
    )
    if last and as_utc(last.created_at) > utcnow() - timedelta(seconds=60):
        if force:
            raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "Please wait a minute before asking for another code.")
        return  # a fresh code was just sent
    db.execute(delete(EmailCode).where(EmailCode.user_id == user.id))
    code = f"{secrets.randbelow(1_000_000):06d}"
    db.add(
        EmailCode(
            user_id=user.id,
            code_hash=hash_email_code(code),
            expires_at=utcnow() + timedelta(minutes=settings.email_code_minutes),
        )
    )
    db.commit()
    body = (
        f"Hello {user.full_name},\n\nYour EduMeet verification code is:\n\n    {code}\n\n"
        f"It works for {settings.email_code_minutes} minutes. If you did not create an EduMeet account, ignore this email.\n"
    )
    if not send_mail(user.email, "Your EduMeet verification code", body):
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "The verification email could not be sent. Ask the administrator to check the email (SMTP) settings.",
        )


@router.post("/verify-email", response_model=TokenResponse)
def verify_email(payload: VerifyEmailRequest, request: Request, db: Session = Depends(get_db)):
    ip, device = _ip(request), payload.device_id.strip()[:64]
    generic = HTTPException(status.HTTP_400_BAD_REQUEST, "That email or code is not correct")
    user = db.scalar(select(User).where(User.email == payload.email.strip().lower()))
    if user is None or user.email_verified:
        raise generic
    _check_lock(user)
    row = db.scalar(select(EmailCode).where(EmailCode.user_id == user.id).order_by(EmailCode.created_at.desc()))
    if row is None or as_utc(row.expires_at) < utcnow():
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "The code expired. Ask for a new one.")
    row.attempts += 1
    if row.attempts > 5:
        db.delete(row)
        db.commit()
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Too many wrong tries. Ask for a new code.")
    if not hmac.compare_digest(row.code_hash, hash_email_code(payload.code)):
        db.commit()  # keep the attempt counter
        log_event("email_code_wrong", user=user, ip=ip)
        raise generic
    user.email_verified = True
    db.execute(delete(EmailCode).where(EmailCode.user_id == user.id))
    db.commit()
    log_event("email_verified", user=user, ip=ip, device_id=device)
    return _finish_login(db, user, device, ip)


@router.post("/resend-code", status_code=204)
def resend_code(payload: ResendRequest, db: Session = Depends(get_db)):
    user = db.scalar(select(User).where(User.email == payload.email.strip().lower()))
    if user is not None and not user.email_verified:
        _send_verification(db, user, force=True)


# ----------------------------- sign up / in / out -----------------------------
@router.post("/register", response_model=TokenResponse, status_code=201)
def register(payload: RegisterRequest, request: Request, db: Session = Depends(get_db)):
    """Public sign-up (student/employee). Checks the address, limits sign-ups, and e-mails a code."""
    ip, device = _ip(request), payload.device_id.strip()[:64]
    if payload.role not in SELF_REGISTER_ROLES:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN, "Self-registration is limited to student and employee accounts."
        )

    def blocked(reason: str, message: str, code: int = status.HTTP_400_BAD_REQUEST) -> HTTPException:
        log_event("register_blocked", email=payload.email, ip=ip, device_id=device, detail=reason)
        return HTTPException(code, message)

    try:
        email, canonical = check_new_email(db, payload.email)
    except EmailRejected as exc:
        raise blocked(exc.reason, exc.message) from None

    if device and settings.max_accounts_per_device > 0:
        used = db.scalar(select(func.count(User.id)).where(User.registered_device_id == device)) or 0
        if used >= settings.max_accounts_per_device:
            raise blocked(
                "device_limit",
                "This computer has already been used to create an account. One person can have only one "
                "account - ask an administrator if you need help.",
                status.HTTP_403_FORBIDDEN,
            )
    if ip and settings.max_registrations_per_ip_per_day > 0:
        since = utcnow() - timedelta(days=1)
        recent = db.scalar(
            select(func.count(User.id)).where(User.registered_ip == ip, User.created_at >= since)
        ) or 0
        if recent >= settings.max_registrations_per_ip_per_day:
            raise blocked(
                "ip_limit", "Too many accounts were created from your network today. Try again tomorrow.",
                status.HTTP_429_TOO_MANY_REQUESTS,
            )
    _ensure_device_free(db, device, None)  # fail before the account is created

    user = User(
        email=email,
        email_canonical=canonical,
        full_name=payload.full_name.strip(),
        hashed_password=hash_password(payload.password),
        role=payload.role.value,
        email_verified=not settings.require_email_verification,
        registered_device_id=device or None,
        registered_ip=ip or None,
    )
    db.add(user)
    db.commit()
    log_event("register_ok", user=user, ip=ip, device_id=device)
    if settings.require_email_verification:
        _send_verification(db, user, force=True)
        return TokenResponse(verification_required=True)
    return _finish_login(db, user, device, ip)


@router.post("/login", response_model=TokenResponse)
def login(payload: LoginRequest, request: Request, db: Session = Depends(get_db)):
    ip, device = _ip(request), payload.device_id.strip()[:64]
    user = db.scalar(select(User).where(User.email == payload.email.strip().lower()))
    if user is None:
        log_event("login_fail", email=payload.email, ip=ip, device_id=device, detail="unknown email")
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Incorrect email or password")
    if _is_locked(user):
        log_event("login_locked", user=user, ip=ip, device_id=device)
        _check_lock(user)
    if not verify_password(payload.password, user.hashed_password):
        _fail(db, user, ip, "login_fail", "wrong password")
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Incorrect email or password")
    if not user.is_active:
        log_event("login_fail", user=user, ip=ip, device_id=device, detail="deactivated")
        raise HTTPException(status.HTTP_403_FORBIDDEN, "This account has been deactivated")
    if not user.email_verified:
        _send_verification(db, user, force=False)
        log_event("login_unverified", user=user, ip=ip, device_id=device)
        return TokenResponse(verification_required=True)

    if user.totp_enabled:
        if not payload.otp.strip():
            return TokenResponse(otp_required=True)  # password OK -> ask for the authenticator code
        if not _verify_second_factor(db, user, payload.otp):
            _fail(db, user, ip, "login_fail_otp", "wrong authenticator/recovery code")
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Incorrect authenticator or recovery code")
    return _finish_login(db, user, device, ip)


@router.post("/logout", status_code=204)
def logout(request: Request, auth: tuple[User, UserSession] = Depends(get_current_auth), db: Session = Depends(get_db)):
    auth[1].revoked = True
    db.commit()
    log_event("logout", user=auth[0], ip=_ip(request), device_id=auth[1].device_id)


@router.get("/me", response_model=UserOut)
def me(user: User = Depends(get_current_user)):
    return user


# ----------------------------- two-step verification -----------------------------
@router.post("/2fa/setup", response_model=TwoFactorSetup)
def two_factor_setup(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Step 1: create a secret and a QR code to scan. Not active until /2fa/enable confirms a code."""
    if user.totp_enabled:
        raise HTTPException(status.HTTP_409_CONFLICT, "Two-step verification is already on")
    user.totp_secret = new_totp_secret()
    db.commit()
    uri = totp_uri(user.totp_secret, user.email)
    return TwoFactorSetup(secret=user.totp_secret, otpauth_uri=uri, qr_png_base64=qr_png_base64(uri))


@router.post("/2fa/enable", response_model=RecoveryCodesOut)
def two_factor_enable(
    payload: CodeRequest, request: Request, user: User = Depends(get_current_user), db: Session = Depends(get_db)
):
    """Step 2: prove the app works by entering a code. Returns recovery codes (shown only once)."""
    if user.totp_enabled:
        raise HTTPException(status.HTTP_409_CONFLICT, "Two-step verification is already on")
    if not user.totp_secret:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Start the setup first")
    step = verify_totp(user.totp_secret, payload.code, 0)
    if step is None:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, "That code is not correct. Check the code in your app and your phone's clock."
        )
    user.totp_enabled, user.last_totp_step = True, step
    codes = _new_recovery_codes(db, user)
    db.commit()
    log_event("2fa_enabled", user=user, ip=_ip(request))
    return RecoveryCodesOut(codes=codes)


def _require_password_and_code(db: Session, user: User, payload: PasswordCodeRequest, ip: str) -> None:
    _check_lock(user)
    if not user.totp_enabled:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Two-step verification is not on")
    if not verify_password(payload.password, user.hashed_password):
        _fail(db, user, ip, "login_fail", "wrong password (security change)")
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Incorrect password")
    if not _verify_second_factor(db, user, payload.code):
        _fail(db, user, ip, "login_fail_otp", "wrong code (security change)")
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Incorrect authenticator or recovery code")


@router.post("/2fa/disable", status_code=204)
def two_factor_disable(
    payload: PasswordCodeRequest, request: Request, user: User = Depends(get_current_user), db: Session = Depends(get_db)
):
    _require_password_and_code(db, user, payload, _ip(request))
    user.totp_enabled, user.totp_secret, user.last_totp_step = False, None, 0
    db.execute(delete(RecoveryCode).where(RecoveryCode.user_id == user.id))
    db.commit()
    log_event("2fa_disabled", user=user, ip=_ip(request))


@router.post("/2fa/recovery-codes", response_model=RecoveryCodesOut)
def two_factor_new_codes(
    payload: PasswordCodeRequest, request: Request, user: User = Depends(get_current_user), db: Session = Depends(get_db)
):
    _require_password_and_code(db, user, payload, _ip(request))
    codes = _new_recovery_codes(db, user)
    db.commit()
    log_event("2fa_new_recovery_codes", user=user, ip=_ip(request))
    return RecoveryCodesOut(codes=codes)


@router.post("/password-reset", status_code=204)
def password_reset(payload: PasswordResetRequest, request: Request, db: Session = Depends(get_db)):
    """Forgot your password? Choose a new one with an authenticator (or recovery) code - no email needed."""
    ip = _ip(request)
    generic = HTTPException(status.HTTP_400_BAD_REQUEST, "That email or code is not correct")
    user = db.scalar(select(User).where(User.email == payload.email.strip().lower()))
    if user is None or not user.totp_enabled or not user.is_active:
        log_event("password_reset_fail", email=payload.email, ip=ip, detail="unknown / no 2FA")
        raise generic
    _check_lock(user)
    if not _verify_second_factor(db, user, payload.code):
        _fail(db, user, ip, "password_reset_fail", "wrong code")
        raise generic
    user.hashed_password = hash_password(payload.new_password)
    user.failed_attempts, user.locked_until = 0, None
    _revoke_sessions(db, user.id)  # everyone signed out; sign in again with the new password
    db.commit()
    log_event("password_reset_ok", user=user, ip=ip)


# ----------------------------- admin-only -----------------------------
@admin_router.get("", response_model=list[UserOut])
def list_users(_: User = Depends(require_admin), db: Session = Depends(get_db)):
    return db.scalars(select(User).order_by(User.created_at.desc())).all()


@admin_router.post("", response_model=UserOut, status_code=201)
def create_user(
    payload: RegisterRequest, request: Request, admin: User = Depends(require_admin), db: Session = Depends(get_db)
):
    """Accounts made by an admin are trusted: verified, and exempt from the disposable-domain rules."""
    email = payload.email.lower()
    canonical = canonical_email(email)
    if _email_taken(db, email) or db.scalar(select(User.id).where(User.email_canonical == canonical)) is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, "Email (or a variant of it) is already registered")
    user = User(
        email=email,
        email_canonical=canonical,
        full_name=payload.full_name.strip(),
        hashed_password=hash_password(payload.password),
        role=payload.role.value,
        email_verified=True,
    )
    db.add(user)
    db.commit()
    log_event("admin_user_created", user=admin, ip=_ip(request), detail=f"{email} as {payload.role.value}")
    return user


@admin_router.patch("/{user_id}", response_model=UserOut)
def update_user(
    user_id: int,
    payload: UserUpdate,
    request: Request,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    user = db.get(User, user_id)
    if not user:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "User not found")
    if user.id == admin.id and (
        payload.is_active is False or (payload.role and payload.role != Role.admin)
    ):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, "You cannot deactivate or demote your own account"
        )
    if payload.full_name is not None:
        user.full_name = payload.full_name.strip()
    if payload.role is not None:
        user.role = payload.role.value
    if payload.is_active is not None:
        user.is_active = payload.is_active
    if payload.password:
        user.hashed_password = hash_password(payload.password)
        _revoke_sessions(db, user.id)
    if payload.reset_2fa:  # the user lost their phone: admin switches 2FA off so they can sign in and set it up again
        user.totp_enabled, user.totp_secret, user.last_totp_step = False, None, 0
        db.execute(delete(RecoveryCode).where(RecoveryCode.user_id == user.id))
        _revoke_sessions(db, user.id)
    db.commit()
    changed = [k for k in payload.model_dump(exclude_unset=True) if k != "password"] + (
        ["password"] if payload.password else []
    )
    log_event("admin_user_updated", user=admin, ip=_ip(request), detail=f"{user.email}: {', '.join(changed)}")
    return user


def ensure_first_admin(db: Session) -> None:
    """Create the bootstrap administrator from .env if no admin exists."""
    if db.scalar(select(User.id).where(User.role == Role.admin.value)) is not None:
        return
    email = settings.first_admin_email.lower()
    db.add(
        User(
            email=email,
            email_canonical=canonical_email(email),
            full_name=settings.first_admin_name,
            hashed_password=hash_password(settings.first_admin_password),
            role=Role.admin.value,
            email_verified=True,
        )
    )
    db.commit()
