"""Chat audience rules, attachment types and poll permissions (no database needed)."""
import asyncio

from backend.filekinds import KIND_EXT, content_type_for, kind_of
from backend.signaling import Peer, _handle, manager


class FakeWS:
    def __init__(self):
        self.sent = []

    async def send_json(self, message):
        self.sent.append(message)

    async def close(self, code=1000):
        pass


def make_peer(pid, name, host=False):
    return Peer(peer_id=pid, ws=FakeWS(), user_id=100 + sum(ord(ch) for ch in pid), name=name, role="teacher" if host else "student", is_host=host)


def chats(peer):
    return [m for m in peer.ws.sent if m["type"] == "chat"]


def test_file_kinds():
    assert kind_of("photo.PNG") == "image" and kind_of("notes.pdf") == "pdf"
    assert kind_of("a.webm", "audio/webm") == "audio" and kind_of("a.webm", "video/webm") == "video"
    assert kind_of("x.exe") == "file" and ".svg" not in KIND_EXT["image"]
    assert content_type_for("audio", ".webm") == "audio/webm" and content_type_for("video", ".webm") == "video/webm"


def test_private_message_reaches_only_host_and_sender():
    async def run():
        host, a, b = make_peer("h", "Host", True), make_peer("a", "Anna"), make_peer("b", "Ben")
        manager.rooms["chatroom"] = {"h": host, "a": a, "b": b}
        await _handle("chatroom", a, {"type": "chat", "text": "I need help", "to": "host"})
        assert len(chats(host)) == 1 and chats(host)[0]["to"] == "host"
        assert len(chats(a)) == 1 and not chats(b)                       # Ben never sees it
        await _handle("chatroom", a, {"type": "chat", "text": "hello all"})
        assert len(chats(b)) == 1 and chats(b)[0]["to"] == "everyone"
        del manager.rooms["chatroom"]["h"]                                # the host is gone
        a.ws.sent.clear()
        await _handle("chatroom", a, {"type": "chat", "text": "anyone?", "to": "host"})
        assert any(m["type"] == "error" for m in a.ws.sent) and not chats(b)[1:]
        manager.rooms.pop("chatroom", None)

    asyncio.run(run())


def test_only_the_host_can_start_or_close_a_poll():
    async def run():
        host, a = make_peer("h", "Host", True), make_peer("a", "Anna")
        manager.rooms["pollroom"] = {"h": host, "a": a}
        await _handle("pollroom", a, {"type": "poll_create", "question": "Q?", "options": ["x", "y"]})
        assert any(m["type"] == "error" for m in a.ws.sent)
        await _handle("pollroom", a, {"type": "poll_close", "poll_id": 1})
        assert sum(m["type"] == "error" for m in a.ws.sent) == 2
        manager.rooms.pop("pollroom", None)

    asyncio.run(run())


def test_host_can_message_selected_people_only():
    async def run():
        host = make_peer("h", "Host", True)
        a, b, c = make_peer("a", "Anna"), make_peer("b", "Ben"), make_peer("c", "Cara")
        manager.rooms["sel"] = {"h": host, "a": a, "b": b, "c": c}
        await _handle("sel", host, {"type": "chat", "text": "Please stay behind", "to": "users", "user_ids": [a.user_id, b.user_id]})
        assert len(chats(a)) == 1 and len(chats(b)) == 1 and not chats(c)             # Cara never sees it
        assert chats(a)[0]["to"] == "users" and chats(a)[0]["to_count"] == 2 and "to_names" not in chats(a)[0]
        assert sorted(chats(host)[0]["to_names"]) == ["Anna", "Ben"]                  # the host sees who got it
        await _handle("sel", host, {"type": "chat", "text": "x", "to": "users", "user_ids": []})
        assert any(m["type"] == "error" for m in host.ws.sent)                        # nobody chosen
        await _handle("sel", a, {"type": "chat", "text": "sneaky", "to": "users", "user_ids": [b.user_id]})
        assert any(m["type"] == "error" for m in a.ws.sent) and len(chats(b)) == 1    # students cannot pick people
        manager.rooms.pop("sel", None)

    asyncio.run(run())
