"""Shared FastAPI dependencies: DB session, current user/session, role checks."""
from datetime import timedelta
from typing import Iterator

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from backend.database import SessionLocal
from backend.models import HOST_ROLES, Role, User, UserSession, as_utc, utcnow
from backend.security import decode_access_token

bearer = HTTPBearer(auto_error=False)
TOUCH_EVERY = timedelta(seconds=20)


def get_db() -> Iterator[Session]:
    with SessionLocal() as db:
        yield db


def authenticate(db: Session, token: str) -> tuple[User, UserSession] | None:
    """Validate a JWT *and* its session (not revoked). Also refreshes the session's last_seen."""
    payload = decode_access_token(token)
    if not payload or not payload.get("sid"):
        return None
    session = db.get(UserSession, str(payload["sid"]))
    if session is None or session.revoked or str(session.user_id) != str(payload.get("sub")):
        return None
    user = db.get(User, session.user_id)
    if user is None or not user.is_active:
        return None
    now = utcnow()
    if as_utc(session.last_seen) < now - TOUCH_EVERY:
        session.last_seen = now
        db.commit()
    return user, session


def user_from_token(db: Session, token: str) -> User | None:
    """Used by the WebSocket endpoint."""
    result = authenticate(db, token)
    return result[0] if result else None


def get_current_auth(
    creds: HTTPAuthorizationCredentials | None = Depends(bearer),
    db: Session = Depends(get_db),
) -> tuple[User, UserSession]:
    unauthorized = HTTPException(
        status.HTTP_401_UNAUTHORIZED,
        "Your session has ended or is not valid. Please sign in again.",
        headers={"WWW-Authenticate": "Bearer"},
    )
    if creds is None:
        raise unauthorized
    result = authenticate(db, creds.credentials)
    if result is None:
        raise unauthorized
    return result


def get_current_user(auth: tuple[User, UserSession] = Depends(get_current_auth)) -> User:
    return auth[0]


def require_roles(*roles: Role):
    allowed = {r.value for r in roles}

    def checker(user: User = Depends(get_current_user)) -> User:
        if user.role not in allowed:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Your role is not allowed to do this")
        return user

    return checker


require_admin = require_roles(Role.admin)
require_host_role = require_roles(*HOST_ROLES)
