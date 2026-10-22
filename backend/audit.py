"""Security / activity log (shown to admins in Monitor -> Security log)."""
from backend.database import SessionLocal
from backend.models import AuditEvent


def log_event(kind: str, *, user=None, email: str = "", ip: str = "", device_id: str = "", detail: str = "") -> None:
    """Write one event using its own DB session so it is kept even if the request later fails. Never raises."""
    try:
        with SessionLocal() as db:
            db.add(
                AuditEvent(
                    kind=kind[:40],
                    user_id=user.id if user is not None else None,
                    email=(email or (user.email if user is not None else ""))[:255],
                    ip=(ip or "")[:64],
                    device_id=(device_id or "")[:64],
                    detail=(detail or "")[:500],
                )
            )
            db.commit()
    except Exception:  # noqa: BLE001  (logging must never break a request)
        pass
