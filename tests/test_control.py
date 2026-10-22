"""Remote-control consent rules (no database needed)."""
import asyncio

from backend.signaling import Peer, _handle, manager


class FakeWS:
    def __init__(self):
        self.sent = []

    async def send_json(self, message):
        self.sent.append(message)

    async def close(self, code=1000):
        pass


def make_peer(pid, name, screen=False):
    return Peer(peer_id=pid, ws=FakeWS(), user_id=1, name=name, role="student", is_host=False, screen_on=screen)


def types(peer):
    return [m["type"] for m in peer.ws.sent]


def test_control_requires_consent_and_is_revoked():
    move = {"type": "control_input", "to": "s", "data": {"t": "move", "x": 0.5, "y": 0.5}}

    async def run():
        sharer, ctrl = make_peer("s", "Sharer", screen=True), make_peer("c", "Controller")
        manager.rooms["room"] = {"s": sharer, "c": ctrl}

        await _handle("room", ctrl, move)                                    # no consent yet
        assert "control_input" not in types(sharer)

        await _handle("room", ctrl, {"type": "control_request", "to": "s"})
        assert "control_request" in types(sharer)
        await _handle("room", ctrl, {"type": "control_response", "to": "s", "granted": True})  # self-approve
        await _handle("room", ctrl, move)
        assert "control_input" not in types(sharer)

        await _handle("room", sharer, {"type": "control_response", "to": "c", "granted": True})
        assert any(m["type"] == "control_response" and m["granted"] for m in ctrl.ws.sent)
        await _handle("room", ctrl, move)                                    # now allowed
        assert "control_input" in types(sharer)

        await _handle("room", sharer, {"type": "media_state", "cam": False, "screen": False})
        assert "control_stopped" in types(ctrl)                              # sharing stopped -> revoked
        sharer.ws.sent.clear()
        await _handle("room", ctrl, move)
        assert "control_input" not in types(sharer)
        manager.rooms.pop("room", None)

    asyncio.run(run())


def test_cannot_request_control_without_screen_share():
    async def run():
        target, ctrl = make_peer("t", "Target", screen=False), make_peer("c", "Controller")
        manager.rooms["room2"] = {"t": target, "c": ctrl}
        await _handle("room2", ctrl, {"type": "control_request", "to": "t"})
        assert "control_request" not in types(target)
        assert "error" in types(ctrl)
        manager.rooms.pop("room2", None)

    asyncio.run(run())
