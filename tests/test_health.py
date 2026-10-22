"""Smoke tests that do NOT need a running PostgreSQL server."""
import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from backend.main import app

# No `with` block -> the lifespan (table creation / admin seeding) is not executed.
client = TestClient(app)


def test_health():
    r = client.get("/api/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_room_page_is_served():
    r = client.get("/room/abc-defg-hij")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
    assert "RTCPeerConnection" in r.text


def test_protected_routes_require_auth():
    assert client.get("/api/auth/me").status_code == 401
    assert client.get("/api/meetings").status_code == 401
    assert client.get("/api/meetings/ice-config").status_code == 401


def test_websocket_rejects_bad_token():
    with pytest.raises(WebSocketDisconnect) as exc:
        with client.websocket_connect("/ws/meetings/abc-defg-hij?token=not-a-jwt") as ws:
            ws.receive_json()
    assert exc.value.code == 4401


def test_device_test_page_is_served():
    r = client.get("/device-test")
    assert r.status_code == 200
    assert "getUserMedia" in r.text
