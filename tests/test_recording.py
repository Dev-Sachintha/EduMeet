"""Recording rules (no database needed)."""
import asyncio

from backend.signaling import Peer, _handle, manager


class FakeWS:
    def __init__(self):
        self.sent = []

    async def send_json(self, message):
        self.sent.append(message)

    async def close(self, code=1000):
        pass


def make_peer(pid, name, host=False):
    return Peer(peer_id=pid, ws=FakeWS(), user_id=1, name=name, role="teacher" if host else "student", is_host=host)


def types(peer):
    return [m["type"] for m in peer.ws.sent]


def test_recording_rules():
    async def run():
        host, other_host, student = make_peer("h", "Host", True), make_peer("o", "Admin", True), make_peer("s", "Stu")
        manager.rooms["rec"] = {"h": host, "o": other_host, "s": student}

        await _handle("rec", student, {"type": "recording", "on": True})          # students cannot record
        assert "error" in types(student) and "recording" not in types(host)

        await _handle("rec", host, {"type": "recording", "on": True})             # everyone is notified
        assert all("recording" in types(p) for p in (host, other_host, student))

        await _handle("rec", other_host, {"type": "recording", "on": True})       # only one recorder
        assert "error" in types(other_host)

        await _handle("rec", other_host, {"type": "recording", "on": False})      # only the recorder can stop
        assert manager.recording.get("rec") == "h"

        await manager.leave("rec", "h")                                           # recorder leaves -> stops
        assert "rec" not in manager.recording
        assert any(m["type"] == "recording" and m["on"] is False for m in student.ws.sent)
        manager.rooms.pop("rec", None)

    asyncio.run(run())
