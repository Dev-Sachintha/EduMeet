"""AI helpers and the captions switch (no network, no database, no models needed)."""
import asyncio

import pytest

from backend.ai import _clean_history, _normalize_quiz, _normalize_structured, _normalize_summary
from backend.extract import ALLOWED_EXT, ExtractError, extract_text
from backend.llm import parse_json
from backend.signaling import Peer, _handle, manager
from backend.transcribe import clean_text


def test_parse_json_variants():
    assert parse_json('{"a": 1}') == {"a": 1}
    assert parse_json('```json\n{"a": 2}\n```') == {"a": 2}
    assert parse_json('Sure! Here it is: {"a": 3} hope that helps') == {"a": 3}
    with pytest.raises(ValueError):
        parse_json("no json here")


def test_clean_text_drops_noise():
    assert clean_text("  Hello    world ") == "Hello world"
    assert clean_text("Thanks for watching!") == ""
    assert clean_text(".") == ""


def test_summary_and_quiz_are_normalised():
    s = _normalize_summary({"summary": "S", "key_points": ["a", ""], "action_items": [{"task": "T"}, "plain", 5]})
    assert s["key_points"] == ["a"] and s["action_items"][0]["owner"] == "unknown" and len(s["action_items"]) == 2
    q = _normalize_quiz({"questions": [
        {"question": "Q1", "options": ["a", "b", "c", "d"], "answer_index": 2, "explanation": "e"},
        {"question": "bad", "options": ["a", "b"], "answer_index": 9},
        {"question": "", "options": ["a", "b"], "answer_index": 0},
    ]})
    assert len(q["questions"]) == 1 and q["questions"][0]["answer_index"] == 2


def test_history_must_alternate():
    ok = [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "hello"}]
    assert _clean_history(ok) == ok
    assert _clean_history([{"role": "assistant", "content": "x"}]) == []
    assert _clean_history([{"role": "user", "content": "a"}, {"role": "user", "content": "b"}]) == []


class FakeWS:
    def __init__(self):
        self.sent = []

    async def send_json(self, message):
        self.sent.append(message)

    async def close(self, code=1000):
        pass


def test_only_the_host_switches_captions():
    async def run():
        host = Peer(peer_id="h", ws=FakeWS(), user_id=1, name="Host", role="teacher", is_host=True)
        student = Peer(peer_id="s", ws=FakeWS(), user_id=2, name="Stu", role="student", is_host=False)
        manager.rooms["cc"] = {"h": host, "s": student}
        await _handle("cc", student, {"type": "captions", "on": True})
        assert "cc" not in manager.captions and any(m["type"] == "error" for m in student.ws.sent)
        await _handle("cc", host, {"type": "captions", "on": True})
        assert "cc" in manager.captions and all(any(m["type"] == "captions" and m["on"] for m in p.ws.sent) for p in (host, student))
        await manager.leave("cc", "h")                                   # the host leaves -> captions stop
        assert "cc" not in manager.captions
        manager.rooms.pop("cc", None)

    asyncio.run(run())


def test_structured_questions_are_normalised():
    data = _normalize_structured({"questions": [
        {"stem": "S", "parts": [
            {"label": "(a)", "question": "Define X", "marks": 2, "model_answer": "X is ...", "marking_points": ["a", ""]},
            {"question": "Explain Y", "marks": "bad"},
            {"question": ""},
        ]},
        {"stem": "no parts", "parts": []},
    ]})
    q = data["questions"]
    assert len(q) == 1 and len(q[0]["parts"]) == 2
    assert q[0]["parts"][1]["marks"] == 1 and q[0]["total_marks"] == 3 and q[0]["parts"][1]["label"] == "(b)"


def test_text_files_are_read_and_unknown_types_refused(tmp_path):
    f = tmp_path / "lesson.txt"
    f.write_text("Photosynthesis turns light into chemical energy.", encoding="utf-8")
    kind, text = extract_text(str(f), ".txt", lambda *_: None)
    assert kind == "text" and "Photosynthesis" in text
    assert {".pdf", ".mp4", ".mp3", ".txt"} <= ALLOWED_EXT
    with pytest.raises(ExtractError):
        extract_text(str(f), ".exe", lambda *_: None)
