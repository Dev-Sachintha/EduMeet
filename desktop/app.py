"""Desktop application bootstrap (PySide6)."""
import os
import sys
from urllib.parse import urlparse

# QtWebEngineWidgets must be imported BEFORE QApplication is created (via dashboard).
from desktop.dashboard import Dashboard  # noqa: I001
from desktop.login_window import LoginWindow
from desktop.styles import STYLESHEET

from PySide6.QtWidgets import QApplication


def _configure_webengine(server_url: str) -> None:
    """Camera/microphone need a 'secure context': https:// or localhost.

    For plain-http LAN testing we tell Chromium to treat the server origin as secure.
    Use real HTTPS for anything beyond local testing.
    """
    parsed = urlparse(server_url)
    flags = os.environ.get("QTWEBENGINE_CHROMIUM_FLAGS", "")
    if parsed.scheme == "http" and parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
        flags += f" --unsafely-treat-insecure-origin-as-secure={parsed.scheme}://{parsed.netloc}"
    os.environ["QTWEBENGINE_CHROMIUM_FLAGS"] = flags.strip()


class AppController:
    def __init__(self, server_url: str):
        self.server_url = server_url
        self.login: LoginWindow | None = None
        self.dashboard: Dashboard | None = None

    def show_login(self) -> None:
        self.login = LoginWindow(self.server_url)
        self.login.logged_in.connect(self._on_login)
        self.login.show()

    def _on_login(self, api) -> None:
        self.server_url = api.base_url
        self.dashboard = Dashboard(api)
        self.dashboard.logout_requested.connect(self._on_logout)
        self.dashboard.show()
        if self.login:
            self.login.close()
            self.login = None

    def _on_logout(self) -> None:
        if self.dashboard:
            self.dashboard.close()
            self.dashboard = None
        self.show_login()


def run() -> int:
    server_url = os.getenv("EDUMEET_SERVER_URL", "http://127.0.0.1:8000")
    _configure_webengine(server_url)
    app = QApplication(sys.argv)
    app.setApplicationName("EduMeet")
    app.setStyleSheet(STYLESHEET)
    controller = AppController(server_url)
    controller.show_login()
    return app.exec()
