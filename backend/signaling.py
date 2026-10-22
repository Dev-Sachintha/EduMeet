"""WebRTC signaling over WebSockets (full-mesh topology) + the room page.

How it works
------------
* Each participant opens  ws://server/ws/meetings/{code}?token=<JWT>
* On join the server sends `welcome` (your peer_id + everyone already in the room).
  The NEW joiner creates an RTCPeerConnection + offer for every existing peer;
  the server only relays `signal` messages (SDP offers/answers/ICE candidates).
* Media flows peer-to-peer (browser engine inside the PySide6 window).
  No media touches this server  ->  this is a MESH: fine for ~2-6 people.
  Larger classes need an SFU (see README).

Room state is held in memory, so run ONE server process. For several workers/servers
you would fan messages out through Redis pub/sub (not included).
"""
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Query, WebSocket
from fastapi.responses import FileResponse
from sqlalchemy import select

from backend.audit import log_event
from backend.database import SessionLocal
from backend.deps import user_from_token
from backend.models import Meeting, MeetingParticipant, Role, utcnow

STATIC_DIR = Path(__file__).resolve().parent / "static"

router = APIRouter(tags=["signaling"])
page_router = APIRouter(tags=["room page"])

# WebSocket close codes (4000-4999 are application-defined)
CLOSE_UNAUTHORIZED = 4401
CLOSE_NOT_FOUND = 4404
CLOSE_ENDED = 4410
CLOSE_DUPLICATE = 4409  # same account already in this meeting


@dataclass
class Peer:
    peer_id: str
    ws: WebSocket
    user_id: int
    name: str
    role: str
    is_host: bool
    hand_raised: bool = False
    cam_on: bool = False
    screen_on: bool = False

    def public(self) -> dict:
        return {
            "peer_id": self.peer_id,
            "name": self.name,
            "role": self.role,
            "is_host": self.is_host,
            "hand_raised": self.hand_raised,
            "cam_on": self.cam_on,
            "screen_on": self.screen_on,
        }

    async def send(self, message: dict) -> None:
        try:
            await self.ws.send_json(message)
        except Exception:  # socket already gone; cleanup happens in the receive loop
            pass


class ConnectionManager:
    def __init__(self) -> None:
        self.rooms: dict[str, dict[str, Peer]] = {}
        # Remote control: (controller_peer_id, target_peer_id) pairs, per room
        self.grants: dict[str, set[tuple[str, str]]] = {}
        self.pending: dict[str, set[tuple[str, str]]] = {}
        # Recording: room code -> peer_id of the participant whose app is recording
        self.recording: dict[str, str] = {}
        # Whiteboard: room code -> {"items": [...], "locked": bool}  (in memory, lost when the room empties)
        self.whiteboard: dict[str, dict] = {}
        # AI captions: room code -> peer_id of the host/admin who switched them on
        self.captions: dict[str, str] = {}

    async def join(self, code: str, peer: Peer) -> None:
        room = self.rooms.setdefault(code, {})
        existing = [p.public() for p in room.values()]
        room[peer.peer_id] = peer
        await peer.send(
            {"type": "welcome", "peer_id": peer.peer_id, "is_host": peer.is_host,
             "you": peer.public(), "peers": existing,
             "recording": code in self.recording, "captions": code in self.captions}
        )
        board = self.whiteboard.get(code)
        if board and (board["items"] or board["locked"]):
            await peer.send({"type": "wb_state", "items": board["items"], "locked": board["locked"]})
        await self.broadcast(code, {"type": "peer_joined", "peer": peer.public()}, exclude=peer.peer_id)

    async def leave(self, code: str, peer_id: str) -> None:
        room = self.rooms.get(code)
        if not room or peer_id not in room:
            return
        del room[peer_id]
        await self.drop_control(code, peer_id, "Participant left")
        was_recording = self.recording.get(code) == peer_id
        if was_recording:
            self.recording.pop(code, None)  # the recording lives in that person's app and ends with them
        was_captions = self.captions.get(code) == peer_id
        if was_captions:
            self.captions.pop(code, None)
        if not room:
            self.rooms.pop(code, None)
            self.grants.pop(code, None)
            self.pending.pop(code, None)
            self.whiteboard.pop(code, None)
            self.captions.pop(code, None)
        else:
            await self.broadcast(code, {"type": "peer_left", "peer_id": peer_id})
            if was_recording:
                await self.broadcast(code, {"type": "recording", "on": False, "peer_id": peer_id, "by": "The recorder"})
            if was_captions:
                await self.broadcast(code, {"type": "captions", "on": False, "by": "The host left, so AI captions"})

    async def broadcast(self, code: str, message: dict, exclude: str | None = None) -> None:
        for peer in list(self.rooms.get(code, {}).values()):
            if peer.peer_id != exclude:
                await peer.send(message)

    async def send_to(self, code: str, peer_id: str, message: dict) -> None:
        peer = self.rooms.get(code, {}).get(peer_id)
        if peer:
            await peer.send(message)

    def board(self, code: str) -> dict:
        return self.whiteboard.setdefault(code, {"items": [], "locked": False})

    # ---------- remote-control bookkeeping (consent is enforced here, on the server) ----------
    def control_exists(self, code: str, controller: str, target: str) -> bool:
        pair = (controller, target)
        return pair in self.grants.get(code, ()) or pair in self.pending.get(code, ())

    def has_grant(self, code: str, controller: str, target: str) -> bool:
        return (controller, target) in self.grants.get(code, ())

    def add_pending(self, code: str, controller: str, target: str) -> None:
        self.pending.setdefault(code, set()).add((controller, target))

    def take_pending(self, code: str, controller: str, target: str) -> bool:
        pairs = self.pending.get(code, set())
        if (controller, target) in pairs:
            pairs.discard((controller, target))
            return True
        return False

    def add_grant(self, code: str, controller: str, target: str) -> None:
        self.grants.setdefault(code, set()).add((controller, target))

    def _discard(self, code: str, pair: tuple[str, str]) -> bool:
        removed = False
        for store in (self.grants, self.pending):
            pairs = store.get(code)
            if pairs and pair in pairs:
                pairs.discard(pair)
                removed = True
        return removed

    async def stop_control(self, code: str, a: str, b: str, reason: str = "Remote control ended") -> None:
        for pair in ((a, b), (b, a)):
            if self._discard(code, pair):
                msg = {"type": "control_stopped", "controller": pair[0], "target": pair[1], "reason": reason}
                await self.send_to(code, pair[0], msg)
                await self.send_to(code, pair[1], msg)

    async def drop_control(self, code: str, peer_id: str, reason: str) -> None:
        pairs = {p for store in (self.grants, self.pending) for p in store.get(code, ()) if peer_id in p}
        for controller, target in pairs:
            await self.stop_control(code, controller, target, reason)

    async def end_room(self, code: str) -> None:
        self.grants.pop(code, None)
        self.pending.pop(code, None)
        self.recording.pop(code, None)
        self.whiteboard.pop(code, None)
        self.captions.pop(code, None)
        peers = list(self.rooms.pop(code, {}).values())
        for peer in peers:
            await peer.send({"type": "meeting_ended"})
        for peer in peers:
            try:
                await peer.ws.close(code=1000)
            except Exception:
                pass


_WB_TOOLS = {"pen", "highlighter", "eraser", "shape"}
_WB_SHAPES = {"line", "arrow", "darrow", "rect", "circle", "diamond"}
_WB_MAX_ITEMS = 3000


def _clean_wb_item(raw, owner: str) -> dict | None:
    """Validate/normalise a whiteboard item from a client (never trust the network)."""
    if not isinstance(raw, dict) or raw.get("tool") not in _WB_TOOLS:
        return None
    points = raw.get("pts")
    if not isinstance(points, list) or not 1 <= len(points) <= 2000:
        return None
    clean_points = []
    try:
        for p in points:
            x, y = float(p[0]), float(p[1])
            clean_points.append([round(min(max(x, 0.0), 1.0), 4), round(min(max(y, 0.0), 1.0), 4)])
        width = min(max(float(raw.get("width", 4)), 1.0), 80.0)
    except (TypeError, ValueError, IndexError, KeyError):
        return None
    color = str(raw.get("color", "#111827"))
    item = {
        "id": str(raw.get("id", ""))[:40],
        "owner": owner,
        "tool": raw["tool"],
        "color": color if re.fullmatch(r"#[0-9a-fA-F]{3,8}", color) else "#111827",
        "width": width,
        "pts": clean_points,
    }
    if item["tool"] == "shape":
        if raw.get("shape") not in _WB_SHAPES or len(clean_points) < 2:
            return None
        item["shape"] = raw["shape"]
        item["fill"] = raw.get("fill") if raw.get("fill") in (0, 1, 2) else 0
        item["pts"] = clean_points[:2]
    return item


manager = ConnectionManager()


# ----------------------------- room page -----------------------------
@page_router.get("/room/{code}", include_in_schema=False)
def room_page(code: str) -> FileResponse:
    """Self-contained WebRTC UI. The desktop app loads this inside QWebEngineView."""
    return FileResponse(
        STATIC_DIR / "room.html", media_type="text/html", headers={"Cache-Control": "no-store"}
    )


@page_router.get("/device-test", include_in_schema=False)
def device_test_page() -> FileResponse:
    """Microphone / camera / speaker check, opened from the desktop app."""
    return FileResponse(
        STATIC_DIR / "device_test.html", media_type="text/html", headers={"Cache-Control": "no-store"}
    )


# ----------------------------- DB helpers (sync, short) -----------------------------
def _mark_ended(code: str) -> None:
    with SessionLocal() as db:
        meeting = db.scalar(select(Meeting).where(Meeting.code == code))
        if meeting and meeting.status != "ended":
            meeting.status = "ended"
            meeting.ended_at = utcnow()
            db.commit()


def _record_leave(participant_id: int | None) -> None:
    if participant_id is None:
        return
    with SessionLocal() as db:
        row = db.get(MeetingParticipant, participant_id)
        if row:
            row.left_at = utcnow()
            db.commit()


# ----------------------------- websocket -----------------------------
@router.websocket("/ws/meetings/{code}")
async def meeting_socket(ws: WebSocket, code: str, token: str = Query(default="")):
    await ws.accept()
    code = code.lower()

    # ---- authenticate + authorise
    with SessionLocal() as db:
        user = user_from_token(db, token)
        if user is None:
            await ws.close(code=CLOSE_UNAUTHORIZED)
            return
        meeting = db.scalar(select(Meeting).where(Meeting.code == code))
        if meeting is None:
            await ws.close(code=CLOSE_NOT_FOUND)
            return
        if meeting.status == "ended":
            await ws.close(code=CLOSE_ENDED)
            return
        if any(p.user_id == user.id for p in manager.rooms.get(code, {}).values()):
            log_event("join_duplicate", user=user, ip=ws.client.host if ws.client else "", detail=f"meeting {code}")
            await ws.close(code=CLOSE_DUPLICATE)  # one account cannot be in the same meeting twice
            return
        if meeting.status == "scheduled":
            meeting.status = "live"
        participant = MeetingParticipant(meeting_id=meeting.id, user_id=user.id)
        db.add(participant)
        db.commit()
        participant_id = participant.id
        peer = Peer(
            peer_id=uuid.uuid4().hex[:12],
            ws=ws,
            user_id=user.id,
            name=user.full_name,
            role=user.role,
            is_host=(meeting.host_id == user.id or user.role == Role.admin.value),
        )

    await manager.join(code, peer)

    try:
        while True:
            msg = await ws.receive_json()
            await _handle(code, peer, msg)
    except Exception:  # WebSocketDisconnect, bad JSON, ... -> treat as leaving
        pass
    finally:
        await manager.leave(code, peer.peer_id)
        _record_leave(participant_id)


async def _handle(code: str, peer: Peer, msg: dict) -> None:
    kind = msg.get("type")

    if kind == "signal":  # relay SDP / ICE to one peer
        target, data = msg.get("to"), msg.get("data")
        if isinstance(target, str) and isinstance(data, dict):
            await manager.send_to(code, target, {"type": "signal", "from": peer.peer_id, "data": data})

    elif kind == "chat":
        text = str(msg.get("text", "")).strip()[:2000]
        if text:
            await manager.broadcast(
                code,
                {"type": "chat", "from": peer.peer_id, "name": peer.name, "text": text,
                 "ts": datetime.now(timezone.utc).isoformat()},
            )

    elif kind == "raise_hand":
        peer.hand_raised = bool(msg.get("raised"))
        await manager.broadcast(code, {"type": "hand", "peer_id": peer.peer_id, "raised": peer.hand_raised})

    elif kind == "media_state":  # camera / screen-share state (avatar instead of frozen frame, control button)
        peer.cam_on = bool(msg.get("cam"))
        was_sharing = peer.screen_on
        peer.screen_on = bool(msg.get("screen"))
        await manager.broadcast(
            code,
            {"type": "media_state", "peer_id": peer.peer_id, "cam": peer.cam_on, "screen": peer.screen_on},
        )
        if was_sharing and not peer.screen_on:
            await manager.drop_control(code, peer.peer_id, "Screen sharing stopped")

    elif kind == "recording":  # host/admin only; everyone in the room is notified
        if not peer.is_host:
            await peer.send({"type": "error", "message": "Only the host can record."})
            return
        on = bool(msg.get("on"))
        current = manager.recording.get(code)
        if on:
            if current and current != peer.peer_id:
                await peer.send({"type": "error", "message": "Someone else is already recording."})
                return
            manager.recording[code] = peer.peer_id
        else:
            if current != peer.peer_id:
                return  # only the person whose app is recording can stop it
            manager.recording.pop(code, None)
        await manager.broadcast(code, {"type": "recording", "on": on, "peer_id": peer.peer_id, "by": peer.name})

    elif kind == "captions":  # host/admin switches AI captions on/off for everyone (everyone is told)
        if not peer.is_host:
            await peer.send({"type": "error", "message": "Only the host can do that."})
            return
        on = bool(msg.get("on"))
        if on:
            manager.captions[code] = peer.peer_id
        else:
            manager.captions.pop(code, None)
        await manager.broadcast(code, {"type": "captions", "on": on, "by": peer.name})

    # ---- shared whiteboard: items are validated, stored (for late joiners) and relayed
    elif kind == "wb_item":
        board = manager.board(code)
        if board["locked"] and not peer.is_host:
            await peer.send({"type": "error", "message": "The host has locked the whiteboard."})
            return
        item = _clean_wb_item(msg.get("item"), peer.peer_id)
        if item is None or len(board["items"]) >= _WB_MAX_ITEMS:
            return
        board["items"].append(item)
        await manager.broadcast(code, {"type": "wb_item", "item": item, "from": peer.peer_id}, exclude=peer.peer_id)

    elif kind == "wb_undo":  # you can remove your own items; the host can remove any
        board, target = manager.board(code), str(msg.get("id"))
        for index, existing in enumerate(board["items"]):
            if existing["id"] == target and (existing["owner"] == peer.peer_id or peer.is_host):
                del board["items"][index]
                await manager.broadcast(code, {"type": "wb_remove", "id": target})
                break

    elif kind in {"wb_clear", "wb_lock"}:
        if not peer.is_host:
            await peer.send({"type": "error", "message": "Only the host can do that."})
            return
        board = manager.board(code)
        if kind == "wb_clear":
            board["items"].clear()
            await manager.broadcast(code, {"type": "wb_clear", "by": peer.name})
        else:
            board["locked"] = bool(msg.get("locked"))
            await manager.broadcast(code, {"type": "wb_lock", "locked": board["locked"]})

    # ---- remote control: request -> target approves -> input relayed only while approved
    elif kind == "control_request":
        target = manager.rooms.get(code, {}).get(str(msg.get("to")))
        if target is None or target.peer_id == peer.peer_id:
            return
        if not target.screen_on:
            await peer.send({"type": "error", "message": f"{target.name} is not sharing their screen."})
            return
        if manager.control_exists(code, peer.peer_id, target.peer_id):
            return
        manager.add_pending(code, peer.peer_id, target.peer_id)
        await target.send({"type": "control_request", "from": peer.peer_id, "name": peer.name})

    elif kind == "control_response":  # sent by the TARGET (the person sharing)
        controller_id = str(msg.get("to"))
        if not manager.take_pending(code, controller_id, peer.peer_id):
            return  # nobody asked, or already answered: a controller can never approve itself
        granted = bool(msg.get("granted")) and peer.screen_on
        if granted:
            manager.add_grant(code, controller_id, peer.peer_id)
        await manager.send_to(
            code, controller_id,
            {"type": "control_response", "from": peer.peer_id, "name": peer.name, "granted": granted},
        )

    elif kind == "control_input":
        target_id, data = str(msg.get("to")), msg.get("data")
        if manager.has_grant(code, peer.peer_id, target_id) and isinstance(data, dict) and len(str(data)) < 400:
            await manager.send_to(code, target_id, {"type": "control_input", "from": peer.peer_id, "data": data})

    elif kind == "control_stop":  # either side may stop at any time
        await manager.stop_control(code, peer.peer_id, str(msg.get("to")), "Remote control ended")

    elif kind in {"mute_all", "kick", "end_meeting"}:
        if not peer.is_host:
            await peer.send({"type": "error", "message": "Only the host can do that."})
            return
        if kind == "mute_all":
            await manager.broadcast(code, {"type": "mute_all", "by": peer.name}, exclude=peer.peer_id)
        elif kind == "kick":
            victim = manager.rooms.get(code, {}).get(str(msg.get("peer_id")))
            if victim and victim.peer_id != peer.peer_id:
                await victim.send({"type": "kicked"})
                try:
                    await victim.ws.close(code=1000)
                except Exception:
                    pass
        else:  # end_meeting
            _mark_ended(code)
            await manager.end_room(code)
