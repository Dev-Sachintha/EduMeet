"""File sharing + meeting recordings (stored on local disk under UPLOAD_DIR)."""
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, UploadFile, status
from fastapi.responses import FileResponse
from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

from backend.config import settings
from backend.deps import get_current_user, get_db
from backend.meetings import get_meeting_or_404
from backend.models import Meeting, MeetingFile, MeetingParticipant, Role, User
from backend.schemas import FileOut

router = APIRouter(prefix="/api", tags=["files"])

BLOCKED_EXTENSIONS = {".exe", ".bat", ".cmd", ".com", ".msi", ".scr", ".ps1", ".vbs", ".js", ".jar", ".dll"}
RECORDING_EXTENSIONS = {".webm", ".mp4", ".mkv"}
CHUNK = 1024 * 1024


def _can_access(db: Session, user: User, meeting: Meeting) -> bool:
    """Host, admins, and anyone who has joined the meeting."""
    if user.role == Role.admin.value or meeting.host_id == user.id:
        return True
    joined = db.scalar(
        select(MeetingParticipant.id).where(
            MeetingParticipant.meeting_id == meeting.id, MeetingParticipant.user_id == user.id
        )
    )
    return joined is not None


def _to_out(f: MeetingFile) -> FileOut:
    return FileOut(
        id=f.id, meeting_id=f.meeting_id, original_name=f.original_name, size=f.size,
        content_type=f.content_type, uploader_name=f.uploader.full_name, created_at=f.created_at,
    )


async def _save_upload(file: UploadFile, limit_mb: int) -> tuple[str, int]:
    """Stream an upload to disk, enforcing a size limit. Returns (stored_name, size)."""
    stored = uuid.uuid4().hex
    path = settings.upload_path / stored
    limit = limit_mb * 1024 * 1024
    size = 0
    try:
        with open(path, "wb") as out:
            while chunk := await file.read(CHUNK):
                size += len(chunk)
                if size > limit:
                    raise HTTPException(
                        status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, f"File exceeds the {limit_mb} MB limit"
                    )
                out.write(chunk)
    except Exception:
        path.unlink(missing_ok=True)
        raise
    return stored, size


@router.post("/meetings/{code}/files", response_model=FileOut, status_code=201)
async def upload_file(
    code: str, file: UploadFile, user: User = Depends(get_current_user), db: Session = Depends(get_db)
):
    meeting = get_meeting_or_404(db, code)
    if not _can_access(db, user, meeting):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Join the meeting first")

    original = Path(file.filename or "file").name[:200] or "file"
    if Path(original).suffix.lower() in BLOCKED_EXTENSIONS:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "This file type is not allowed")

    stored, size = await _save_upload(file, settings.max_upload_mb)
    row = MeetingFile(
        meeting_id=meeting.id, uploader_id=user.id, original_name=original, stored_name=stored,
        content_type=file.content_type or "application/octet-stream", size=size,
    )
    db.add(row)
    db.commit()
    row.uploader = user
    return _to_out(row)


@router.post("/meetings/{code}/recordings", response_model=FileOut, status_code=201)
async def upload_recording(
    code: str, file: UploadFile, user: User = Depends(get_current_user), db: Session = Depends(get_db)
):
    """Host/admin only. Larger size limit than normal files (MAX_RECORDING_MB)."""
    meeting = get_meeting_or_404(db, code)
    if user.role != Role.admin.value and meeting.host_id != user.id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Only the host can upload recordings")

    original = Path(file.filename or "recording.webm").name[:200] or "recording.webm"
    if Path(original).suffix.lower() not in RECORDING_EXTENSIONS:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Recordings must be .webm, .mp4 or .mkv")

    stored, size = await _save_upload(file, settings.max_recording_mb)
    row = MeetingFile(
        meeting_id=meeting.id, uploader_id=user.id, original_name=original, stored_name=stored,
        content_type=file.content_type or "video/webm", size=size,
    )
    db.add(row)
    db.commit()
    row.uploader = user
    return _to_out(row)


@router.get("/meetings/{code}/files", response_model=list[FileOut])
def list_files(code: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    meeting = get_meeting_or_404(db, code)
    if not _can_access(db, user, meeting):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Join the meeting first")
    rows = db.scalars(
        select(MeetingFile)
        .options(joinedload(MeetingFile.uploader))
        .where(MeetingFile.meeting_id == meeting.id)
        .order_by(MeetingFile.created_at.desc())
    ).all()
    return [_to_out(r) for r in rows]


@router.get("/files/{file_id}/download")
def download_file(file_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    row = db.get(MeetingFile, file_id)
    if not row:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "File not found")
    meeting = db.get(Meeting, row.meeting_id)
    if not _can_access(db, user, meeting):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Join the meeting first")
    path = settings.upload_path / row.stored_name
    if not path.exists():
        raise HTTPException(status.HTTP_404_NOT_FOUND, "File is missing on the server")
    return FileResponse(path, filename=row.original_name, media_type=row.content_type)
