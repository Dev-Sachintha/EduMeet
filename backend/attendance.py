"""Live attendance: automatic marking from joins/leaves, roll call, manual marks, roster and chart data.

Statuses (for people on the roster):
  present_now = connected right now      left = was here, attended >= ATTENDANCE_MIN_PERCENT of the session
  left_early  = was here, attended less   absent = never joined       excused = the teacher excused them
"""
import asyncio
import math
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from backend.config import settings
from backend.database import SessionLocal
from backend.deps import get_current_user, get_db, require_host_role
from backend.meetings import get_meeting_or_404
from backend.models import (
    AttendanceMark, AttendanceRollcall, Meeting, MeetingParticipant, MeetingRoster, Role, User, as_utc, utcnow,
)
from backend.signaling import manager

router = APIRouter(prefix="/api/attendance", tags=["attendance"])
page_router = APIRouter(tags=["attendance page"])
STATIC_DIR = Path(__file__).resolve().parent / "static"


# ============================ pure calculations (unit-tested, no database) ============================
def merge_intervals(intervals):
    merged: list[tuple[datetime, datetime]] = []
    for start, end in sorted((s, e) for s, e in intervals if e > s):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def total_minutes(merged, start: datetime, end: datetime) -> float:
    seconds = 0.0
    for s, e in merged:
        lo, hi = max(s, start), min(e, end)
        if hi > lo:
            seconds += (hi - lo).total_seconds()
    return seconds / 60


def build_rows(people, roster, host_id, intervals, connected, marks, rc_total, rc_answered,
               session_start, end, grace_min, min_pct):
    session_minutes = max(1.0, (end - session_start).total_seconds() / 60)
    rows = []
    for uid in (set(roster) | set(intervals) | set(connected)) - {host_id}:
        info = people.get(uid, {"name": f"User {uid}", "email": "", "role": ""})
        merged = merge_intervals(intervals.get(uid, []))
        minutes = total_minutes(merged, session_start, end)
        percent = min(100, round(minutes / session_minutes * 100))
        here = uid in connected
        joined = bool(merged) or here
        first = merged[0][0] if merged else None
        mark = marks.get(uid, "")
        if mark == "excused":
            state = "excused"
        elif mark == "absent":
            state = "absent"
        elif here:
            state = "present_now"
        elif joined:
            state = "left" if (mark in ("present", "late") or percent >= min_pct) else "left_early"
        else:
            state = "left" if mark in ("present", "late") else "absent"   # never joined
        late = mark == "late" or bool(first and first > session_start + timedelta(minutes=grace_min))
        rows.append({
            "user_id": uid, "name": info["name"], "email": info["email"], "role": info["role"],
            "on_roster": uid in roster, "status": state, "late": late, "mark": mark,
            "first_join": first.isoformat() if first else None, "minutes": round(minutes, 1), "percent": percent,
            "joins": len(intervals.get(uid, [])), "rollcalls_answered": rc_answered.get(uid, 0), "rollcalls_total": rc_total,
        })
    order = {"present_now": 0, "left": 1, "left_early": 2, "absent": 3, "excused": 4}
    rows.sort(key=lambda r: (order[r["status"]], r["name"].lower()))
    return rows


def summarize(rows, roster):
    counts = {k: 0 for k in ("present_now", "left", "left_early", "absent", "excused")}
    late = guests = 0
    for r in rows:
        if r["on_roster"]:
            counts[r["status"]] += 1
        else:
            guests += 1 if r["status"] != "absent" else 0
        late += 1 if r["late"] and r["status"] != "absent" else 0
    attended = counts["present_now"] + counts["left"]
    denominator = len(roster) - counts["excused"]
    rate = round(attended / denominator * 100) if roster and denominator > 0 else None
    return {**counts, "late": late, "guests": guests, "roster_size": len(roster), "attended": attended, "rate": rate}


def build_timeline(session_start, end, merged_by_user, roster, host_id, max_points=120):
    total = max(1, int((end - session_start).total_seconds() // 60))
    step = max(1, math.ceil(total / max_points))
    points = []
    for minute in list(range(0, total + 1, step)) + ([total] if total % step else []):
        at = session_start + timedelta(minutes=minute)
        people = in_roster = 0
        for uid, merged in merged_by_user.items():
            if uid != host_id and any(s <= at <= e for s, e in merged):
                people += 1
                in_roster += uid in roster
        points.append({"minute": minute, "people": people, "roster": in_roster})
    return points


# ============================ database -> report ============================
def compute_report(db: Session, meeting: Meeting, with_trend: bool = False) -> dict:
    now = utcnow()
    connected = {p.user_id for p in manager.rooms.get(meeting.code, {}).values()}
    parts = db.scalars(select(MeetingParticipant).where(MeetingParticipant.meeting_id == meeting.id)).all()
    roster = set(db.scalars(select(MeetingRoster.user_id).where(MeetingRoster.meeting_id == meeting.id)).all())
    empty = {"rows": [], "timeline": [], "trend": [], "rollcall": None,
             "session": {"title": meeting.title, "code": meeting.code, "status": meeting.status, "start": None, "end": None,
                         "minutes": 0, "grace": settings.attendance_grace_minutes, "min_percent": settings.attendance_min_percent}}
    if not parts:
        return {**empty, "summary": summarize([], roster)}

    intervals: dict[int, list] = defaultdict(list)
    for p in parts:
        start = as_utc(p.joined_at)
        end = as_utc(p.left_at) if p.left_at else (now if p.user_id in connected else start)
        intervals[p.user_id].append((start, end))
    host_starts = [s for s, _ in intervals.get(meeting.host_id, [])]
    session_start = min(host_starts) if host_starts else min(s for ivs in intervals.values() for s, _ in ivs)
    end = as_utc(meeting.ended_at) if meeting.status == "ended" and meeting.ended_at else now
    end = max(end, session_start)

    marks: dict[int, str] = {}
    for mk in db.scalars(select(AttendanceMark).where(AttendanceMark.meeting_id == meeting.id, AttendanceMark.kind == "manual").order_by(AttendanceMark.id)):
        marks[mk.user_id] = mk.mark
    rc_total = db.scalar(select(func.count(AttendanceRollcall.id)).where(AttendanceRollcall.meeting_id == meeting.id)) or 0
    rc_answered = dict(db.execute(
        select(AttendanceMark.user_id, func.count(AttendanceMark.id))
        .where(AttendanceMark.meeting_id == meeting.id, AttendanceMark.kind == "rollcall").group_by(AttendanceMark.user_id)
    ).all())
    ids = set(roster) | set(intervals) | connected
    people = {u.id: {"name": u.full_name, "email": u.email, "role": u.role} for u in db.scalars(select(User).where(User.id.in_(ids)))} if ids else {}

    rows = build_rows(people, roster, meeting.host_id, intervals, connected, marks, rc_total, rc_answered,
                      session_start, end, settings.attendance_grace_minutes, settings.attendance_min_percent)
    merged_by_user = {uid: merge_intervals(ivs) for uid, ivs in intervals.items()}
    state = manager.rollcalls.get(meeting.code)
    rollcall = None
    if state and manager.rollcall_public(meeting.code):
        rollcall = {**manager.rollcall_public(meeting.code), "answered": len(state["acked"]), "in_room": len(connected - {meeting.host_id})}

    report = {
        "session": {**empty["session"], "start": session_start.isoformat(), "end": end.isoformat(),
                    "minutes": round((end - session_start).total_seconds() / 60, 1)},
        "summary": summarize(rows, roster), "rows": rows, "rollcall": rollcall, "trend": [],
        "timeline": build_timeline(session_start, end, merged_by_user, roster, meeting.host_id),
    }
    if with_trend:
        recent = db.scalars(select(Meeting).where(Meeting.host_id == meeting.host_id).order_by(Meeting.created_at.desc()).limit(10)).all()
        for other in reversed(recent):
            snap = report if other.id == meeting.id else compute_report(db, other)
            if snap["summary"]["roster_size"] and snap["session"]["start"] and snap["summary"]["rate"] is not None:
                report["trend"].append({"code": other.code, "label": f"{as_utc(other.created_at):%d %b} {other.title[:14]}", "rate": snap["summary"]["rate"]})
    return report


def _host_meeting(db: Session, user: User, code: str) -> Meeting:
    meeting = get_meeting_or_404(db, code)
    if user.role != Role.admin.value and meeting.host_id != user.id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Only the host can see and change attendance.")
    return meeting


# ============================ page ============================
@page_router.get("/attendance/{code}", include_in_schema=False)
def attendance_page(code: str) -> FileResponse:
    return FileResponse(STATIC_DIR / "attendance.html", media_type="text/html", headers={"Cache-Control": "no-store"})


# ============================ API ============================
@router.get("/users/search")
def search_users(q: str = "", user: User = Depends(require_host_role), db: Session = Depends(get_db)):
    """Find accounts to add to a roster (hosts only)."""
    stmt = select(User).where(User.is_active.is_(True), User.id != user.id).order_by(User.full_name).limit(15)
    if q.strip():
        like = f"%{q.strip()}%"
        stmt = stmt.where(User.full_name.ilike(like) | User.email.ilike(like))
    return [{"id": u.id, "name": u.full_name, "email": u.email, "role": u.role} for u in db.scalars(stmt).all()]


@router.get("/{code}")
def report(code: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    return compute_report(db, _host_meeting(db, user, code), with_trend=True)


class RosterIn(BaseModel):
    user_ids: list[int] = Field(max_length=300)


@router.post("/{code}/roster", status_code=204)
def roster_add(code: str, payload: RosterIn, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    meeting = _host_meeting(db, user, code)
    have = set(db.scalars(select(MeetingRoster.user_id).where(MeetingRoster.meeting_id == meeting.id)).all())
    valid = set(db.scalars(select(User.id).where(User.id.in_(payload.user_ids), User.is_active.is_(True))).all())
    for uid in (valid - have) - {meeting.host_id}:
        db.add(MeetingRoster(meeting_id=meeting.id, user_id=uid))
    db.commit()


@router.post("/{code}/roster/joined")
def roster_add_joined(code: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Add everyone who has joined so far (quick way to build the roster during the first class)."""
    meeting = _host_meeting(db, user, code)
    have = set(db.scalars(select(MeetingRoster.user_id).where(MeetingRoster.meeting_id == meeting.id)).all())
    joined = set(db.scalars(select(MeetingParticipant.user_id).where(MeetingParticipant.meeting_id == meeting.id)).all())
    added = (joined - have) - {meeting.host_id}
    for uid in added:
        db.add(MeetingRoster(meeting_id=meeting.id, user_id=uid))
    db.commit()
    return {"added": len(added)}


class CopyIn(BaseModel):
    from_code: str


@router.post("/{code}/roster/copy")
def roster_copy(code: str, payload: CopyIn, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    meeting = _host_meeting(db, user, code)
    source = _host_meeting(db, user, payload.from_code)
    have = set(db.scalars(select(MeetingRoster.user_id).where(MeetingRoster.meeting_id == meeting.id)).all())
    wanted = set(db.scalars(select(MeetingRoster.user_id).where(MeetingRoster.meeting_id == source.id)).all())
    for uid in (wanted - have) - {meeting.host_id}:
        db.add(MeetingRoster(meeting_id=meeting.id, user_id=uid))
    db.commit()
    return {"added": len((wanted - have) - {meeting.host_id})}


@router.delete("/{code}/roster/{user_id}", status_code=204)
def roster_remove(code: str, user_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    meeting = _host_meeting(db, user, code)
    db.execute(delete(MeetingRoster).where(MeetingRoster.meeting_id == meeting.id, MeetingRoster.user_id == user_id))
    db.commit()


class MarkIn(BaseModel):
    user_id: int
    mark: str = Field(pattern="^(present|late|absent|excused|clear)$")


@router.post("/{code}/mark", status_code=204)
def set_mark(code: str, payload: MarkIn, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """The teacher overrides the automatic result for one person ('clear' = back to automatic)."""
    meeting = _host_meeting(db, user, code)
    db.execute(delete(AttendanceMark).where(
        AttendanceMark.meeting_id == meeting.id, AttendanceMark.user_id == payload.user_id, AttendanceMark.kind == "manual"))
    if payload.mark != "clear":
        db.add(AttendanceMark(meeting_id=meeting.id, user_id=payload.user_id, kind="manual", mark=payload.mark))
    db.commit()


# ----------------------------- roll call -----------------------------
class RollcallIn(BaseModel):
    action: str = Field(pattern="^(start|stop)$")
    seconds: int = Field(default=60, ge=15, le=600)


async def _close_rollcall(code: str) -> None:
    state = manager.rollcalls.pop(code, None)
    if not state:
        return
    with SessionLocal() as db:
        row = db.get(AttendanceRollcall, state["id"])
        if row:
            row.ended_at = utcnow()
            db.commit()
    await manager.broadcast(code, {"type": "rollcall", "on": False, "id": state["id"], "answered": len(state["acked"])})


async def _auto_close(code: str, rollcall_id: int, seconds: int) -> None:
    await asyncio.sleep(seconds + 1)
    state = manager.rollcalls.get(code)
    if state and state["id"] == rollcall_id:
        await _close_rollcall(code)


@router.post("/{code}/rollcall")
async def rollcall(code: str, payload: RollcallIn, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Roll call: everyone in the room gets an 'I am here' button for N seconds (proves they are really there)."""
    meeting = _host_meeting(db, user, code)
    if payload.action == "stop":
        await _close_rollcall(meeting.code)
        return {"ok": True}
    if not manager.rooms.get(meeting.code):
        raise HTTPException(status.HTTP_409_CONFLICT, "Nobody is in the room yet. Join the meeting first.")
    if manager.rollcall_public(meeting.code):
        raise HTTPException(status.HTTP_409_CONFLICT, "A roll call is already running.")
    row = AttendanceRollcall(meeting_id=meeting.id, seconds=payload.seconds)
    db.add(row)
    db.commit()
    manager.rollcalls[meeting.code] = {"id": row.id, "ends": utcnow() + timedelta(seconds=payload.seconds), "acked": set()}
    await manager.broadcast(meeting.code, {"type": "rollcall", "on": True, "id": row.id, "seconds_left": payload.seconds, "by": user.full_name})
    asyncio.create_task(_auto_close(meeting.code, row.id, payload.seconds))
    return {"ok": True, "id": row.id}


@router.post("/{code}/rollcall/ack")
async def rollcall_ack(code: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    meeting = get_meeting_or_404(db, code)
    state = manager.rollcalls.get(meeting.code)
    if not state or not manager.rollcall_public(meeting.code):
        raise HTTPException(status.HTTP_409_CONFLICT, "There is no roll call running.")
    if not any(p.user_id == user.id for p in manager.rooms.get(meeting.code, {}).values()):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "You must be in the meeting.")
    if user.id not in state["acked"]:
        db.add(AttendanceMark(meeting_id=meeting.id, user_id=user.id, kind="rollcall", mark="present", rollcall_id=state["id"]))
        db.commit()
        state["acked"].add(user.id)
    return {"ok": True}
