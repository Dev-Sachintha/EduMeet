"""Meeting CRUD + ICE configuration."""
import secrets
import string

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

from backend.config import settings
from backend.deps import get_current_user, get_db, require_host_role
from backend.models import Meeting, Role, User, utcnow
from backend.schemas import MeetingCreate, MeetingOut
from backend.signaling import manager

router = APIRouter(prefix="/api/meetings", tags=["meetings"])

_ALPHABET = string.ascii_lowercase


def _random_code() -> str:
    pick = lambda n: "".join(secrets.choice(_ALPHABET) for _ in range(n))  # noqa: E731
    return f"{pick(3)}-{pick(4)}-{pick(3)}"  # e.g. abc-defg-hij


def meeting_to_out(m: Meeting) -> MeetingOut:
    return MeetingOut(
        id=m.id,
        code=m.code,
        title=m.title,
        description=m.description or "",
        host_id=m.host_id,
        host_name=m.host.full_name,
        status=m.status,
        scheduled_at=m.scheduled_at,
        created_at=m.created_at,
        ended_at=m.ended_at,
    )


def get_meeting_or_404(db: Session, code: str) -> Meeting:
    meeting = db.scalar(
        select(Meeting).options(joinedload(Meeting.host)).where(Meeting.code == code.lower())
    )
    if not meeting:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Meeting not found")
    return meeting


# NOTE: declared before "/{code}" so "ice-config" is not captured as a meeting code.
@router.get("/ice-config")
def ice_config(_: User = Depends(get_current_user)):
    """STUN/TURN servers handed to the WebRTC room page."""
    return {"iceServers": settings.ice_servers}


@router.post("", response_model=MeetingOut, status_code=201)
def create_meeting(
    payload: MeetingCreate, user: User = Depends(require_host_role), db: Session = Depends(get_db)
):
    code = _random_code()
    while db.scalar(select(Meeting.id).where(Meeting.code == code)) is not None:
        code = _random_code()
    meeting = Meeting(
        code=code,
        title=payload.title.strip(),
        description=payload.description.strip(),
        host_id=user.id,
        scheduled_at=payload.scheduled_at,
    )
    db.add(meeting)
    db.commit()
    return meeting_to_out(get_meeting_or_404(db, code))


@router.get("", response_model=list[MeetingOut])
def list_meetings(
    mine: bool = False, user: User = Depends(get_current_user), db: Session = Depends(get_db)
):
    """Open (scheduled/live) meetings, or only the ones you host when mine=true."""
    query = select(Meeting).options(joinedload(Meeting.host)).order_by(Meeting.created_at.desc())
    query = query.where(Meeting.host_id == user.id) if mine else query.where(Meeting.status != "ended")
    return [meeting_to_out(m) for m in db.scalars(query.limit(100)).all()]


@router.get("/{code}", response_model=MeetingOut)
def get_meeting(code: str, _: User = Depends(get_current_user), db: Session = Depends(get_db)):
    return meeting_to_out(get_meeting_or_404(db, code))


@router.post("/{code}/end", response_model=MeetingOut)
async def end_meeting(
    code: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)
):
    meeting = get_meeting_or_404(db, code)
    if meeting.host_id != user.id and user.role != Role.admin.value:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Only the host or an admin can end this meeting")
    if meeting.status != "ended":
        meeting.status = "ended"
        meeting.ended_at = utcnow()
        db.commit()
    await manager.end_room(meeting.code)  # kick everyone out of the live room
    return meeting_to_out(meeting)
