"""Whiteboard validation + permissions (no database needed)."""
import asyncio

from backend.signaling import Peer, _clean_wb_item, _handle, manager


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


def test_item_validation():
    good = {"id": "a-1", "tool": "pen", "color": "#ff0000", "width": 5, "pts": [[0.1, 0.2], [2, -1]]}
    item = _clean_wb_item(good, "p1")
    assert item["pts"][1] == [1.0, 0.0] and item["owner"] == "p1"          # clamped to the board
    assert _clean_wb_item({**good, "tool": "laser"}, "p1") is None
    assert _clean_wb_item({**good, "pts": []}, "p1") is None
    assert _clean_wb_item({**good, "pts": [["x", 1]]}, "p1") is None
    assert _clean_wb_item({**good, "color": "red;evil"}, "p1")["color"] == "#111827"
    assert _clean_wb_item({"id": "s", "tool": "shape", "shape": "star", "pts": [[0, 0], [1, 1]]}, "p1") is None
    shape = _clean_wb_item({"id": "s", "tool": "shape", "shape": "rect", "fill": 2, "pts": [[0, 0], [1, 1], [0.5, 0.5]]}, "p1")
    assert shape["fill"] == 2 and len(shape["pts"]) == 2


def test_whiteboard_permissions():
    stroke = {"id": "s-1", "tool": "pen", "color": "#000000", "width": 4, "pts": [[0.1, 0.1], [0.2, 0.2]]}

    async def run():
        host, student = make_peer("h", "Host", True), make_peer("s", "Stu")
        manager.rooms["wb"] = {"h": host, "s": student}
        manager.whiteboard.pop("wb", None)

        await _handle("wb", student, {"type": "wb_item", "item": stroke})        # everyone can draw by default
        assert "wb_item" in types(host)

        await _handle("wb", student, {"type": "wb_clear"})                       # only the host can clear / lock
        assert "error" in types(student) and manager.board("wb")["items"]

        await _handle("wb", host, {"type": "wb_lock", "locked": True})
        student.ws.sent.clear()
        await _handle("wb", student, {"type": "wb_item", "item": {**stroke, "id": "s-2"}})
        assert "error" in types(student) and len(manager.board("wb")["items"]) == 1

        await _handle("wb", student, {"type": "wb_undo", "id": "s-1"})           # own items can be undone
        assert not manager.board("wb")["items"]

        await _handle("wb", host, {"type": "wb_clear"})
        manager.rooms.pop("wb", None)
        manager.whiteboard.pop("wb", None)

    asyncio.run(run())


def test_text_and_table_items():
    box = {"id": "t1", "tool": "text", "color": "#111827", "width": 28, "pts": [[0.1, 0.1], [0.4, 0.2]], "text": "Hello"}
    assert _clean_wb_item(box, "p1")["text"] == "Hello"
    assert _clean_wb_item({**box, "text": "   "}, "p1") is None
    table = {"id": "tb", "tool": "table", "color": "#111827", "width": 3, "pts": [[0.1, 0.1], [0.6, 0.5]],
             "rows": 2, "cols": 3, "header": True, "cells": [["a", "b"], ["c"]]}
    clean = _clean_wb_item(table, "p1")
    assert clean["cells"] == [["a", "b", ""], ["c", "", ""]] and clean["header"] is True
    assert _clean_wb_item({**table, "rows": 99}, "p1") is None
    assert _clean_wb_item({**table, "cols": "x"}, "p1") is None


def test_only_owner_or_host_can_edit_a_text_box():
    box = {"id": "t1", "tool": "text", "color": "#111827", "width": 28, "pts": [[0.1, 0.1], [0.4, 0.2]], "text": "Hello"}

    async def run():
        host, a, b = make_peer("h", "Host", True), make_peer("a", "A"), make_peer("b", "B")
        manager.rooms["wbt"] = {"h": host, "a": a, "b": b}
        manager.whiteboard.pop("wbt", None)
        await _handle("wbt", a, {"type": "wb_item", "item": box})
        await _handle("wbt", b, {"type": "wb_update", "item": {**box, "text": "hacked"}})        # not the owner
        assert manager.board("wbt")["items"][0]["text"] == "Hello"
        await _handle("wbt", a, {"type": "wb_update", "item": {**box, "text": "Edited"}})        # the owner
        assert manager.board("wbt")["items"][0]["text"] == "Edited" and "wb_update" in types(b)
        await _handle("wbt", host, {"type": "wb_update", "item": {**box, "text": "By host"}})    # the host
        assert manager.board("wbt")["items"][0]["text"] == "By host"
        manager.rooms.pop("wbt", None)
        manager.whiteboard.pop("wbt", None)

    asyncio.run(run())
