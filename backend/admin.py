"""Monitoring for administrators: who is online, security log, suspicious sign-ups, blocked domains."""
import re
from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from backend.audit import log_event
from backend.config import settings
from backend.deps import get_db, require_admin
from backend.models import AuditEvent, BlockedDomain, User, UserSession, utcnow

router = APIRouter(prefix="/api/admin", tags=["monitor (admin)"])


def _cutoff():
    return utcnow() - timedelta(seconds=settings.session_idle_seconds)


@router.get("/stats")
def stats(_: User = Depends(require_admin), db: Session = Depends(get_db)):
    day = utcnow() - timedelta(days=1)

    def count(stmt) -> int:
        return db.scalar(stmt) or 0

    return {
        "users": count(select(func.count(User.id))),
        "unverified": count(select(func.count(User.id)).where(User.email_verified.is_(False))),
        "online": count(
            select(func.count(UserSession.id)).where(UserSession.revoked.is_(False), UserSession.last_seen > _cutoff())
        ),
        "failed_logins_24h": count(
            select(func.count(AuditEvent.id)).where(
                AuditEvent.kind.in_(["login_fail", "login_fail_otp"]), AuditEvent.created_at >= day
            )
        ),
        "blocked_signups_24h": count(
            select(func.count(AuditEvent.id)).where(AuditEvent.kind == "register_blocked", AuditEvent.created_at >= day)
        ),
        "locked_accounts": count(select(func.count(User.id)).where(User.locked_until > utcnow())),
    }


@router.get("/sessions")
def online_sessions(_: User = Depends(require_admin), db: Session = Depends(get_db)):
    rows = db.execute(
        select(UserSession, User)
        .join(User, User.id == UserSession.user_id)
        .where(UserSession.revoked.is_(False), UserSession.last_seen > _cutoff())
        .order_by(UserSession.last_seen.desc())
    ).all()
    return [
        {
            "id": s.id, "user_id": u.id, "name": u.full_name, "email": u.email, "role": u.role,
            "device": (s.device_id or "")[:8], "created_at": s.created_at, "last_seen": s.last_seen,
        }
        for s, u in rows
    ]


@router.post("/sessions/{session_id}/revoke", status_code=204)
def revoke_session(
    session_id: str, request: Request, admin: User = Depends(require_admin), db: Session = Depends(get_db)
):
    session = db.get(UserSession, session_id)
    if session is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Session not found")
    session.revoked = True
    db.commit()
    owner = db.get(User, session.user_id)
    log_event(
        "admin_session_revoked", user=admin, ip=request.client.host if request.client else "",
        detail=f"signed out {owner.email if owner else session.user_id}",
    )


@router.get("/events")
def events(
    kind: str = "", q: str = "", limit: int = 200,
    _: User = Depends(require_admin), db: Session = Depends(get_db),
):
    stmt = select(AuditEvent).order_by(AuditEvent.id.desc()).limit(min(max(limit, 1), 1000))
    if kind:
        stmt = stmt.where(AuditEvent.kind == kind)
    if q.strip():
        like = f"%{q.strip()}%"
        stmt = stmt.where(
            AuditEvent.email.ilike(like) | AuditEvent.ip.ilike(like)
            | AuditEvent.device_id.ilike(like) | AuditEvent.detail.ilike(like)
        )
    return [
        {
            "id": e.id, "created_at": e.created_at, "kind": e.kind, "email": e.email,
            "ip": e.ip, "device": (e.device_id or "")[:8], "detail": e.detail,
        }
        for e in db.scalars(stmt).all()
    ]


@router.get("/suspicious")
def suspicious(_: User = Depends(require_admin), db: Session = Depends(get_db)):
    """Signs that one person may be using several accounts."""
    out: list[dict] = []

    def emails(stmt) -> str:
        return ", ".join(db.scalars(stmt).all())

    for device, n in db.execute(
        select(User.registered_device_id, func.count(User.id))
        .where(User.registered_device_id.is_not(None)).group_by(User.registered_device_id)
        .having(func.count(User.id) > 1)
    ).all():
        out.append({
            "kind": "Several accounts created on one computer", "key": device[:8], "count": n,
            "accounts": emails(select(User.email).where(User.registered_device_id == device)),
        })
    distinct_users = func.count(func.distinct(UserSession.user_id))
    for device, n in db.execute(
        select(UserSession.device_id, distinct_users).where(UserSession.device_id != "")
        .group_by(UserSession.device_id).having(distinct_users > 1)
    ).all():
        out.append({
            "kind": "Several accounts signed in on one computer", "key": device[:8], "count": n,
            "accounts": emails(
                select(User.email).join(UserSession, UserSession.user_id == User.id)
                .where(UserSession.device_id == device).distinct()
            ),
        })
    for ip, n in db.execute(
        select(User.registered_ip, func.count(User.id))
        .where(User.registered_ip.is_not(None)).group_by(User.registered_ip).having(func.count(User.id) > 2)
    ).all():
        out.append({
            "kind": "Many accounts created from one network address", "key": ip, "count": n,
            "accounts": emails(select(User.email).where(User.registered_ip == ip)),
        })
    return out


class DomainIn(BaseModel):
    domain: str


@router.get("/blocked-domains")
def list_blocked(_: User = Depends(require_admin), db: Session = Depends(get_db)):
    return [
        {"id": d.id, "domain": d.domain, "created_at": d.created_at}
        for d in db.scalars(select(BlockedDomain).order_by(BlockedDomain.domain)).all()
    ]


@router.post("/blocked-domains", status_code=201)
def add_blocked(
    payload: DomainIn, request: Request, admin: User = Depends(require_admin), db: Session = Depends(get_db)
):
    domain = payload.domain.strip().lower().lstrip("@")
    if not re.fullmatch(r"[a-z0-9-]+(\.[a-z0-9-]+)+", domain):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Enter a domain like example.com")
    if db.scalar(select(BlockedDomain.id).where(BlockedDomain.domain == domain)) is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, "That domain is already blocked")
    row = BlockedDomain(domain=domain)
    db.add(row)
    db.commit()
    log_event("domain_blocked", user=admin, ip=request.client.host if request.client else "", detail=domain)
    return {"id": row.id, "domain": row.domain, "created_at": row.created_at}


@router.delete("/blocked-domains/{domain_id}", status_code=204)
def remove_blocked(
    domain_id: int, request: Request, admin: User = Depends(require_admin), db: Session = Depends(get_db)
):
    row = db.get(BlockedDomain, domain_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found")
    domain = row.domain
    db.delete(row)
    db.commit()
    log_event("domain_unblocked", user=admin, ip=request.client.host if request.client else "", detail=domain)
