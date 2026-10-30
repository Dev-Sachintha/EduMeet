"""File sharing + meeting recordings (stored on local disk under UPLOAD_DIR)."""
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, UploadFile, status
from fastapi.responses import FileResponse
from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

from backend.config import settings
from backend.deps import get_current_user, get_db
from backend.filekinds import KIND_EXT, content_type_for, kind_of
from backend.meetings import get_meeting_or_404
from backend.models import Meeting, MeetingFile, MeetingParticipant, Role, User
from backend.schemas import FileOut
from backend.security import make_file_link_token, read_file_link_token

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


def _can_see_file(user: User, meeting: Meeting, row: MeetingFile) -> bool:
    """'host only' files: host, admins and the sender. 'users' files: the sender, admins and the chosen people."""
    audience = getattr(row, "audience", "everyone")
    if audience == "everyone":
        return True
    if user.role == Role.admin.value or row.uploader_id == user.id:
        return True
    if audience == "host":
        return meeting.host_id == user.id
    return str(user.id) in (getattr(row, "recipients", "") or "").split(",")


def _looks_like(kind: str, head: bytes) -> bool:
    """Cheap content check so a renamed file cannot pretend to be an image or a PDF."""
    if kind == "pdf":
        return head.startswith(b"%PDF")
    if kind == "image":
        return head.startswith((b"\x89PNG", b"\xff\xd8\xff", b"GIF8", b"BM")) or (head[:4] == b"RIFF" and head[8:12] == b"WEBP")
    return True


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


@router.post("/meetings/{code}/chat-upload")
async def chat_upload(
    code: str, file: UploadFile, kind: str, audience: str = "everyone", user_ids: str = "",
    user: User = Depends(get_current_user), db: Session = Depends(get_db),
):
    """An image / PDF / audio / video (or voice message) attached to a chat message."""
    if kind not in KIND_EXT:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Unknown attachment type")
    audience = audience if audience in ("host", "users") else "everyone"
    meeting = get_meeting_or_404(db, code)
    if not _can_access(db, user, meeting):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Join the meeting first")
    recipients = ""
    if audience == "users":
        if user.role != Role.admin.value and meeting.host_id != user.id:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Only the host can send to selected people.")
        ids = sorted({int(x) for x in user_ids.split(",") if x.strip().isdigit()})[:100]
        if not ids:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Choose at least one person.")
        recipients = ",".join(str(i) for i in ids)
    original = Path(file.filename or "file").name[:200] or "file"
    ext = Path(original).suffix.lower()
    if ext not in KIND_EXT[kind]:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"That is not a supported {kind} file ({', '.join(sorted(KIND_EXT[kind]))}).")
    stored, size = await _save_upload(file, settings.max_chat_upload_mb)
    with open(settings.upload_path / stored, "rb") as handle:
        head = handle.read(16)
    if not _looks_like(kind, head):
        (settings.upload_path / stored).unlink(missing_ok=True)
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"That file is not really a {kind}.")
    row = MeetingFile(
        meeting_id=meeting.id, uploader_id=user.id, original_name=original, stored_name=stored,
        content_type=content_type_for(kind, ext), size=size, audience=audience, recipients=recipients,
    )
    db.add(row)
    db.commit()
    return {"id": row.id, "name": original, "size": size, "kind": kind, "audience": audience, "recipients": recipients}


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
    return [_to_out(r) for r in rows if _can_see_file(user, meeting, r)]


@router.get("/files/{file_id}/download")
def download_file(file_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    row = db.get(MeetingFile, file_id)
    if not row:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "File not found")
    meeting = db.get(Meeting, row.meeting_id)
    if not _can_access(db, user, meeting):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Join the meeting first")
    if not _can_see_file(user, meeting, row):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "This file was shared with the host only")
    path = settings.upload_path / row.stored_name
    if not path.exists():
        raise HTTPException(status.HTTP_404_NOT_FOUND, "File is missing on the server")
    return FileResponse(path, filename=row.original_name, media_type=row.content_type)


def _open_row(db: Session, user: User, file_id: int) -> tuple[MeetingFile, Path]:
    row = db.get(MeetingFile, file_id)
    if not row:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "File not found")
    meeting = db.get(Meeting, row.meeting_id)
    if not _can_access(db, user, meeting) or not _can_see_file(user, meeting, row):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "You cannot open this file")
    path = settings.upload_path / row.stored_name
    if not path.exists():
        raise HTTPException(status.HTTP_404_NOT_FOUND, "File is missing on the server")
    return row, path


@router.get("/files/{file_id}/link")
def file_link(file_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """A 30-minute link the room page uses in <img>, <audio>, <video> tags and for downloads."""
    _open_row(db, user, file_id)
    return {"url": f"/api/files/{file_id}/stream?t={make_file_link_token(user.id, file_id)}"}


@router.get("/files/{file_id}/stream")
def stream_file(file_id: int, t: str = "", download: int = 0, db: Session = Depends(get_db)):
    parsed = read_file_link_token(t)
    if parsed is None or parsed[1] != file_id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "This link has expired. Reload the file.")
    user = db.get(User, parsed[0])
    if user is None or not user.is_active:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Not allowed")
    row, path = _open_row(db, user, file_id)
    kind = kind_of(row.original_name, row.content_type)
    inline = not download and kind in ("image", "pdf", "audio", "video")  # everything else is only ever downloaded
    return FileResponse(
        path, filename=row.original_name, media_type=row.content_type,
        content_disposition_type="inline" if inline else "attachment", headers={"X-Content-Type-Options": "nosniff"},
    )
