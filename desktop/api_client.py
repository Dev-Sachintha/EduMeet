"""HTTP client for the EduMeet backend + a tiny helper for running calls off the UI thread."""
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

import requests
from PySide6.QtCore import QObject, Signal, Slot

HOST_ROLES = {"admin", "teacher", "lecturer", "employee"}


def load_device_id() -> str:
    """A random id stored once per computer/user. The server uses it to enforce 'one account per computer'."""
    path = Path.home() / ".edumeet" / "device_id"
    try:
        if path.exists():
            value = path.read_text().strip()
            if value:
                return value[:64]
        path.parent.mkdir(parents=True, exist_ok=True)
        value = uuid.uuid4().hex
        path.write_text(value)
        return value
    except OSError:
        return uuid.uuid4().hex


class ApiError(Exception):
    """A readable error message suitable for showing to the user."""

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


class ApiClient:
    def __init__(self, base_url: str):
        base = base_url.strip().rstrip("/")
        if not urlparse(base).scheme:
            base = "http://" + base
        self.base_url = base
        self.session = requests.Session()
        self.token: str | None = None
        self.user: dict | None = None
        self.device_id = load_device_id()

    # ---------- plumbing ----------
    @staticmethod
    def _detail(resp: requests.Response) -> str:
        try:
            detail = resp.json().get("detail")
        except ValueError:
            detail = None
        if isinstance(detail, list):  # FastAPI validation errors
            detail = "; ".join(
                f"{'.'.join(str(p) for p in e.get('loc', [])[1:])}: {e.get('msg', '')}" for e in detail
            )
        return str(detail or f"Server returned {resp.status_code}")

    def _request(self, method: str, path: str, timeout: int = 45, **kwargs) -> requests.Response:
        headers = kwargs.pop("headers", {})
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        try:
            resp = self.session.request(
                method, self.base_url + path, headers=headers, timeout=timeout, **kwargs
            )
        except requests.RequestException as exc:
            raise ApiError(f"Cannot reach the server at {self.base_url}.\n{exc}") from exc
        if resp.status_code >= 400:
            raise ApiError(self._detail(resp), resp.status_code)
        return resp

    def _json(self, method: str, path: str, **kwargs) -> Any:
        return self._request(method, path, **kwargs).json()

    # ---------- auth ----------
    def _store_session(self, data: dict) -> dict:
        self.token, self.user = data["access_token"], data["user"]
        return self.user

    def login(self, email: str, password: str, otp: str = "") -> dict:
        """Returns the user, or {"otp_required": True} when the authenticator code is needed next."""
        data = self._json(
            "POST", "/api/auth/login",
            json={"email": email, "password": password, "otp": otp, "device_id": self.device_id},
        )
        if data.get("verification_required"):
            return {"verification_required": True}
        if data.get("otp_required"):
            return {"otp_required": True}
        return self._store_session(data)

    def register(self, email: str, full_name: str, password: str, role: str) -> dict:
        """Returns the user, or {"verification_required": True} when a code was e-mailed."""
        data = self._json(
            "POST", "/api/auth/register",
            json={"email": email, "full_name": full_name, "password": password, "role": role,
                  "device_id": self.device_id},
        )
        if data.get("verification_required"):
            return {"verification_required": True}
        return self._store_session(data)

    def verify_email(self, email: str, code: str) -> dict:
        data = self._json(
            "POST", "/api/auth/verify-email", json={"email": email, "code": code, "device_id": self.device_id}
        )
        return self._store_session(data)

    def resend_code(self, email: str) -> None:
        self._request("POST", "/api/auth/resend-code", json={"email": email})

    def logout(self, timeout: int = 10) -> None:
        self._request("POST", "/api/auth/logout", timeout=timeout)

    def me(self) -> dict:
        self.user = self._json("GET", "/api/auth/me")
        return self.user

    def heartbeat(self) -> bool:
        """True = session alive, False = it ended (signed out / signed in elsewhere). Network errors raise."""
        try:
            self._request("GET", "/api/auth/me")
            return True
        except ApiError as exc:
            if exc.status == 401:
                return False
            raise

    # ---------- admin: monitoring ----------
    def admin_stats(self) -> dict:
        return self._json("GET", "/api/admin/stats")

    def admin_sessions(self) -> list[dict]:
        return self._json("GET", "/api/admin/sessions")

    def admin_revoke_session(self, session_id: str) -> None:
        self._request("POST", f"/api/admin/sessions/{session_id}/revoke")

    def admin_events(self, kind: str = "", q: str = "") -> list[dict]:
        return self._json("GET", "/api/admin/events", params={"kind": kind, "q": q, "limit": 300})

    def admin_suspicious(self) -> list[dict]:
        return self._json("GET", "/api/admin/suspicious")

    def admin_domains(self) -> list[dict]:
        return self._json("GET", "/api/admin/blocked-domains")

    def admin_add_domain(self, domain: str) -> dict:
        return self._json("POST", "/api/admin/blocked-domains", json={"domain": domain})

    def admin_delete_domain(self, domain_id: int) -> None:
        self._request("DELETE", f"/api/admin/blocked-domains/{domain_id}")

    # ---------- two-step verification ----------
    def setup_2fa(self) -> dict:
        return self._json("POST", "/api/auth/2fa/setup")

    def enable_2fa(self, code: str) -> list[str]:
        return self._json("POST", "/api/auth/2fa/enable", json={"code": code})["codes"]

    def disable_2fa(self, password: str, code: str) -> None:
        self._request("POST", "/api/auth/2fa/disable", json={"password": password, "code": code})

    def new_recovery_codes(self, password: str, code: str) -> list[str]:
        return self._json("POST", "/api/auth/2fa/recovery-codes", json={"password": password, "code": code})["codes"]

    def reset_password(self, email: str, code: str, new_password: str) -> None:
        self._request("POST", "/api/auth/password-reset", json={"email": email, "code": code, "new_password": new_password})

    @property
    def can_host(self) -> bool:
        return bool(self.user and self.user["role"] in HOST_ROLES)

    @property
    def is_admin(self) -> bool:
        return bool(self.user and self.user["role"] == "admin")

    # ---------- meetings ----------
    def list_meetings(self, mine: bool = False) -> list[dict]:
        return self._json("GET", "/api/meetings", params={"mine": str(mine).lower()})

    def create_meeting(self, title: str, description: str = "", scheduled_at: str | None = None) -> dict:
        return self._json(
            "POST", "/api/meetings",
            json={"title": title, "description": description, "scheduled_at": scheduled_at},
        )

    def get_meeting(self, code: str) -> dict:
        return self._json("GET", f"/api/meetings/{code.strip().lower()}")

    def end_meeting(self, code: str) -> dict:
        return self._json("POST", f"/api/meetings/{code}/end")

    def room_url(self, code: str, cam: bool = True, mic: bool = True) -> str:
        # The token travels in the URL *fragment*: it is never sent to the server in the request line.
        return f"{self.base_url}/room/{code}#token={self.token}&cam={int(cam)}&mic={int(mic)}"

    # ---------- files ----------
    def list_files(self, code: str) -> list[dict]:
        return self._json("GET", f"/api/meetings/{code}/files")

    def upload_file(self, code: str, path: str) -> dict:
        with open(path, "rb") as fh:
            return self._json(
                "POST", f"/api/meetings/{code}/files", files={"file": (Path(path).name, fh)}, timeout=300
            )

    def download_file(self, file_id: int, dest: str) -> str:
        resp = self._request("GET", f"/api/files/{file_id}/download", timeout=300, stream=True)
        with open(dest, "wb") as out:
            for chunk in resp.iter_content(1024 * 256):
                out.write(chunk)
        return dest

    # ---------- admin: users ----------
    def list_users(self) -> list[dict]:
        return self._json("GET", "/api/users")

    def create_user(self, email: str, full_name: str, password: str, role: str) -> dict:
        return self._json(
            "POST", "/api/users",
            json={"email": email, "full_name": full_name, "password": password, "role": role},
        )

    def update_user(self, user_id: int, **fields) -> dict:
        return self._json("PATCH", f"/api/users/{user_id}", json=fields)


# ---------------------------------------------------------------------------
# Run blocking API calls in a worker thread and deliver the result on the UI thread.
# ---------------------------------------------------------------------------
class _Dispatcher(QObject):
    call = Signal(object)

    def __init__(self) -> None:
        super().__init__()
        self.call.connect(self._invoke)

    @Slot(object)
    def _invoke(self, fn: Callable[[], None]) -> None:
        try:
            fn()
        except RuntimeError:  # the target widget was closed before the result arrived
            pass


_pool = ThreadPoolExecutor(max_workers=4)
_dispatcher: _Dispatcher | None = None


def run_async(
    fn: Callable[..., Any],
    on_done: Callable[[Any], None],
    on_error: Callable[[str], None] | None = None,
    *args,
    **kwargs,
) -> None:
    """Call fn(*args, **kwargs) in a thread; then on_done(result) / on_error(message) in the UI thread."""
    global _dispatcher
    if _dispatcher is None:
        _dispatcher = _Dispatcher()  # must be created on the UI thread (first call is)

    def work() -> None:
        try:
            result = fn(*args, **kwargs)
        except ApiError as exc:
            message = str(exc)
            _dispatcher.call.emit(lambda: on_error and on_error(message))
        except Exception as exc:  # noqa: BLE001
            message = f"Unexpected error: {exc}"
            _dispatcher.call.emit(lambda: on_error and on_error(message))
        else:
            _dispatcher.call.emit(lambda: on_done(result))

    _pool.submit(work)
