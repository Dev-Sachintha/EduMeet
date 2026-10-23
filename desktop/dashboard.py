"""Main window: meetings, file sharing, admin user management, and the video room window."""
import base64
import csv
from datetime import datetime, timezone
from pathlib import Path

from PySide6.QtCore import QDateTime, QFile, QIODevice, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QGuiApplication, QPixmap
from PySide6.QtWebEngineCore import QWebEnginePage, QWebEngineProfile, QWebEngineScript, QWebEngineSettings
from PySide6.QtWebChannel import QWebChannel
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWidgets import (
    QAbstractItemView, QCheckBox, QComboBox, QDateTimeEdit, QDialog, QDialogButtonBox, QFileDialog,
    QFormLayout, QHBoxLayout, QHeaderView, QInputDialog, QLabel, QLineEdit, QMainWindow,
    QMessageBox, QPlainTextEdit, QPushButton, QTableWidget, QTableWidgetItem, QTabWidget,
    QVBoxLayout, QWidget,
)

from desktop.api_client import ApiClient, run_async
from desktop.remote_control import RemoteControlBridge
from desktop.styles import make_secondary

ROLES = ["student", "teacher", "lecturer", "employee", "admin"]


def fmt_dt(value: str | None) -> str:
    if not value:
        return "-"
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if dt.tzinfo is None:  # SQLite drops timezone info; stored values are UTC
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone().strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return value


def fmt_size(num: int) -> str:
    size = float(num)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{num} B"


def make_table(headers: list[str]) -> QTableWidget:
    table = QTableWidget(0, len(headers))
    table.setHorizontalHeaderLabels(headers)
    table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
    table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
    table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
    table.verticalHeader().setVisible(False)
    table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
    return table


def fill_table(table: QTableWidget, rows: list[list[str]]) -> None:
    table.setRowCount(len(rows))
    for r, row in enumerate(rows):
        for c, value in enumerate(row):
            table.setItem(r, c, QTableWidgetItem(str(value)))


# =============================================================================
# Video room (WebRTC runs inside the embedded Chromium engine)
# =============================================================================
_MEDIA_PERMISSIONS = {
    "MediaAudioCapture", "MediaVideoCapture", "MediaAudioVideoCapture",
    "DesktopVideoCapture", "DesktopAudioVideoCapture",
}


class RoomPage(QWebEnginePage):
    leave_requested = Signal()

    def __init__(self, parent=None):
        super().__init__(QWebEngineProfile.defaultProfile(), parent)
        settings = self.settings()
        settings.setAttribute(QWebEngineSettings.WebAttribute.ScreenCaptureEnabled, True)
        settings.setAttribute(QWebEngineSettings.WebAttribute.WebRTCPublicInterfacesOnly, False)

        # Qt >= 6.8 uses permissionRequested; older versions use featurePermissionRequested.
        if hasattr(self, "permissionRequested"):
            self.permissionRequested.connect(self._on_permission)
        else:
            self.featurePermissionRequested.connect(self._on_feature_permission)
        # Qt >= 6.7: screen-share picker. We pick the primary screen (a real picker UI is a TODO).
        if hasattr(self, "desktopMediaRequested"):
            self.desktopMediaRequested.connect(self._on_desktop_media)

    def _on_permission(self, permission) -> None:  # Qt >= 6.8
        if permission.permissionType().name in _MEDIA_PERMISSIONS:
            permission.grant()
        else:
            permission.deny()

    def _on_feature_permission(self, origin, feature) -> None:  # Qt < 6.8
        policy = QWebEnginePage.PermissionPolicy
        allowed = feature.name in _MEDIA_PERMISSIONS
        self.setFeaturePermission(
            origin, feature, policy.PermissionGrantedByUser if allowed else policy.PermissionDeniedByUser
        )

    def _on_desktop_media(self, request) -> None:
        try:
            request.selectScreen(request.screensModel().index(0, 0))
        except Exception:  # noqa: BLE001
            request.cancel()

    def acceptNavigationRequest(self, url, nav_type, is_main_frame):  # noqa: N802
        if url.scheme() == "edumeet":  # room page navigates here when the user clicks Leave
            self.leave_requested.emit()
            return False
        return super().acceptNavigationRequest(url, nav_type, is_main_frame)


class RoomWindow(QWidget):
    def __init__(self, api: ApiClient, meeting: dict, cam: bool = True, mic: bool = True):
        super().__init__()
        self.setWindowTitle(f"EduMeet - {meeting['title']}  ({meeting['code']})")
        self.resize(1200, 760)
        self.view = QWebEngineView(self)
        self.room_page = RoomPage(self.view)
        self.room_page.leave_requested.connect(self.close)
        self.view.setPage(self.room_page)
        self._setup_remote_control()
        self._closing = False
        self._check_done = True
        QWebEngineProfile.defaultProfile().downloadRequested.connect(self._on_download)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.view)
        self.view.load(QUrl(api.room_url(meeting["code"], cam, mic)))

    def _setup_remote_control(self) -> None:
        """Give the room page a `bridge` object (QWebChannel) used for consent-based remote control."""
        self.bridge = RemoteControlBridge(self)
        self.channel = QWebChannel(self)
        self.channel.registerObject("bridge", self.bridge)
        self.room_page.setWebChannel(self.channel)
        # http pages cannot load qrc:// files, so inject qwebchannel.js into the page ourselves.
        js = QFile(":/qtwebchannel/qwebchannel.js")
        if js.open(QIODevice.OpenModeFlag.ReadOnly):
            script = QWebEngineScript()
            script.setName("qwebchannel")
            script.setSourceCode(bytes(js.readAll()).decode("utf-8"))
            script.setInjectionPoint(QWebEngineScript.InjectionPoint.DocumentCreation)
            script.setWorldId(QWebEngineScript.ScriptWorldId.MainWorld)
            script.setRunsOnSubFrames(False)
            self.room_page.scripts().insert(script)
            js.close()

    def _on_download(self, item) -> None:
        """Fallback when a recording / whiteboard image could not be uploaded: let the user pick where to save it."""
        if item.page() is not self.room_page:
            return
        suggested = item.downloadFileName() or "recording.webm"
        path, _ = QFileDialog.getSaveFileName(self, "Save file", suggested, "All files (*)")
        if not path:
            item.cancel()
            return
        target = Path(path)
        item.setDownloadDirectory(str(target.parent))
        item.setDownloadFileName(target.name)
        item.accept()

    def closeEvent(self, event) -> None:  # noqa: N802
        if self._closing:
            self.bridge.shutdown()
            self.view.setUrl(QUrl("about:blank"))  # stops camera/mic and drops the WebSocket
            super().closeEvent(event)
            return
        # Don't lose a recording: ask the page whether one is running or still being saved.
        event.ignore()
        self._check_done = False
        self.room_page.runJavaScript("window.__busy === true", self._on_busy_checked)
        QTimer.singleShot(1500, self._check_timeout)

    def _on_busy_checked(self, busy) -> None:
        self._check_done = True
        if busy:
            answer = QMessageBox.question(
                self, "Recording in progress",
                "A recording is running or still being saved.\n\nStop it, save it and leave the meeting?",
            )
            if answer == QMessageBox.StandardButton.Yes:
                self.room_page.runJavaScript("window.edumeetLeave && window.edumeetLeave()")  # closes when saved
            return
        self._closing = True
        self.close()

    def _check_timeout(self) -> None:
        if not self._check_done and not self._closing:  # page did not answer: never trap the user
            self._closing = True
            self.close()


# =============================================================================
# Dialogs
# =============================================================================
class DeviceTestDialog(QDialog):
    """Camera / microphone / speaker check (served by the backend; choices are remembered for meetings)."""

    def __init__(self, api: ApiClient, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Test microphone & camera")
        self.resize(980, 700)
        self.view = QWebEngineView(self)
        self.test_page = RoomPage(self.view)                # same permission handling as the room
        self.test_page.leave_requested.connect(self.accept)  # the page's Done button
        self.view.setPage(self.test_page)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.view)
        self.view.load(QUrl(api.base_url + "/device-test"))

    def done(self, result: int) -> None:
        self.view.setUrl(QUrl("about:blank"))  # releases camera and microphone
        super().done(result)


class AttendanceDialog(QDialog):
    """Live attendance with pie, bar and line charts (a page served by the backend)."""

    def __init__(self, api: ApiClient, meeting: dict, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"Attendance - {meeting['title']}")
        self.resize(1100, 780)
        self.view = QWebEngineView(self)
        self.att_page = RoomPage(self.view)
        self.view.setPage(self.att_page)
        QWebEngineProfile.defaultProfile().downloadRequested.connect(self._on_download)  # Export CSV
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.view)
        self.view.load(QUrl(f"{api.base_url}/attendance/{meeting['code']}#token={api.token}"))

    def _on_download(self, item) -> None:
        if item.page() is not self.att_page:
            return
        path, _ = QFileDialog.getSaveFileName(self, "Save file", item.downloadFileName() or "attendance.csv", "All files (*)")
        if not path:
            item.cancel()
            return
        target = Path(path)
        item.setDownloadDirectory(str(target.parent))
        item.setDownloadFileName(target.name)
        item.accept()

    def done(self, result: int) -> None:
        self.view.setUrl(QUrl("about:blank"))
        super().done(result)


class NewMeetingDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("New meeting")
        self.setMinimumWidth(420)
        self.title_edit = QLineEdit()
        self.title_edit.setPlaceholderText("e.g. Physics 101 - Lecture 4")
        self.desc_edit = QPlainTextEdit()
        self.desc_edit.setFixedHeight(80)
        self.schedule_cb = QCheckBox("Schedule for later")
        self.when = QDateTimeEdit(QDateTime.currentDateTime())
        self.when.setCalendarPopup(True)
        self.when.setEnabled(False)
        self.schedule_cb.toggled.connect(self.when.setEnabled)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)

        form = QFormLayout(self)
        form.addRow("Title", self.title_edit)
        form.addRow("Description", self.desc_edit)
        form.addRow(self.schedule_cb)
        form.addRow("Starts", self.when)
        form.addRow(buttons)

    def _accept(self) -> None:
        if len(self.title_edit.text().strip()) < 3:
            QMessageBox.warning(self, "EduMeet", "Please enter a title (at least 3 characters).")
            return
        self.accept()

    def payload(self) -> tuple[str, str, str | None]:
        scheduled = None
        if self.schedule_cb.isChecked():
            local = self.when.dateTime().toPython()
            scheduled = local.astimezone(timezone.utc).isoformat()
        return self.title_edit.text().strip(), self.desc_edit.toPlainText().strip(), scheduled


class JoinOptionsDialog(QDialog):
    """Choose camera / microphone state before entering a room."""

    def __init__(self, meeting: dict, cam: bool, mic: bool, parent=None, api: ApiClient | None = None):
        super().__init__(parent)
        self.setWindowTitle("Join meeting")
        self.setMinimumWidth(340)
        title = QLabel(f"<b>{meeting['title']}</b>")
        self.camera = QCheckBox("Join with camera on")
        self.camera.setChecked(cam)
        self.microphone = QCheckBox("Join with microphone on")
        self.microphone.setChecked(mic)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Join")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.addWidget(title)
        layout.addWidget(self.camera)
        layout.addWidget(self.microphone)
        if api is not None:
            test = make_secondary(QPushButton("Test microphone && camera..."))
            test.clicked.connect(lambda: DeviceTestDialog(api, self).exec())
            layout.addWidget(test)
        layout.addWidget(buttons)


class FilesDialog(QDialog):
    def __init__(self, api: ApiClient, meeting: dict, parent=None):
        super().__init__(parent)
        self.api, self.meeting, self.files = api, meeting, []
        self.setWindowTitle(f"Files - {meeting['title']}")
        self.resize(640, 380)

        self.table = make_table(["Name", "Size", "Uploaded by", "Date"])
        self.table.itemSelectionChanged.connect(self._update_buttons)
        self.status = QLabel("")
        self.status.setObjectName("muted")

        self.upload_btn = QPushButton("Upload...")
        self.upload_btn.clicked.connect(self._upload)
        self.download_btn = make_secondary(QPushButton("Download..."))
        self.download_btn.clicked.connect(self._download)
        close_btn = make_secondary(QPushButton("Close"))
        close_btn.clicked.connect(self.accept)

        row = QHBoxLayout()
        row.addWidget(self.upload_btn)
        row.addWidget(self.download_btn)
        row.addStretch()
        row.addWidget(close_btn)
        layout = QVBoxLayout(self)
        layout.addWidget(self.table)
        layout.addWidget(self.status)
        layout.addLayout(row)
        self._update_buttons()
        self.refresh()

    def _update_buttons(self) -> None:
        self.download_btn.setEnabled(self.table.currentRow() >= 0)

    def _error(self, message: str) -> None:
        self.status.setText("")
        QMessageBox.warning(self, "EduMeet", message)

    def refresh(self) -> None:
        run_async(self.api.list_files, self._loaded, self._error, self.meeting["code"])

    def _loaded(self, files: list[dict]) -> None:
        self.files = files
        fill_table(self.table, [
            [f["original_name"], fmt_size(f["size"]), f["uploader_name"], fmt_dt(f["created_at"])]
            for f in files
        ])
        self._update_buttons()

    def _upload(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Choose a file to share")
        if not path:
            return
        self.status.setText("Uploading...")
        run_async(self.api.upload_file, lambda _f: (self.status.setText("Uploaded."), self.refresh()),
                  self._error, self.meeting["code"], path)

    def _download(self) -> None:
        row = self.table.currentRow()
        if row < 0:
            return
        file = self.files[row]
        dest, _ = QFileDialog.getSaveFileName(self, "Save file as", file["original_name"])
        if not dest:
            return
        self.status.setText("Downloading...")
        run_async(self.api.download_file, lambda p: self.status.setText(f"Saved to {p}"),
                  self._error, file["id"], dest)


class UserDialog(QDialog):
    """Create a user (user=None) or edit an existing one."""

    def __init__(self, user: dict | None = None, parent=None):
        super().__init__(parent)
        self.editing = user is not None
        self.setWindowTitle("Edit user" if self.editing else "Add user")
        self.setMinimumWidth(380)
        self.name = QLineEdit(user["full_name"] if user else "")
        self.email = QLineEdit(user["email"] if user else "")
        self.email.setEnabled(not self.editing)
        self.password = QLineEdit()
        self.password.setEchoMode(QLineEdit.EchoMode.Password)
        self.password.setPlaceholderText("Leave blank to keep current" if self.editing else "At least 8 characters")
        self.role = QComboBox()
        self.role.addItems(ROLES)
        if user:
            self.role.setCurrentText(user["role"])
        self.active = QCheckBox("Account is active")
        self.active.setChecked(user["is_active"] if user else True)
        self.reset2fa = QCheckBox("Reset two-step verification (user lost their phone)")

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        form = QFormLayout(self)
        form.addRow("Full name", self.name)
        form.addRow("Email", self.email)
        form.addRow("Password", self.password)
        form.addRow("Role", self.role)
        if self.editing:
            form.addRow(self.active)
            form.addRow(self.reset2fa)
        form.addRow(buttons)

    def values(self) -> dict:
        data = {"full_name": self.name.text().strip(), "role": self.role.currentText(),
                "password": self.password.text()}
        if self.editing:
            data["is_active"] = self.active.isChecked()
            data["reset_2fa"] = self.reset2fa.isChecked()
        else:
            data["email"] = self.email.text().strip()
        return data


# =============================================================================
# Tabs
# =============================================================================
class PasswordCodeDialog(QDialog):
    """Ask for the account password plus an authenticator (or recovery) code."""

    def __init__(self, title: str, text: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setMinimumWidth(420)
        note = QLabel(text)
        note.setWordWrap(True)
        self.password = QLineEdit()
        self.password.setEchoMode(QLineEdit.EchoMode.Password)
        self.code = QLineEdit()
        self.code.setPlaceholderText("6-digit code or recovery code")
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        form = QFormLayout(self)
        form.addRow(note)
        form.addRow("Password", self.password)
        form.addRow("Code", self.code)
        form.addRow(buttons)


class RecoveryCodesDialog(QDialog):
    def __init__(self, codes: list[str], parent=None):
        super().__init__(parent)
        self.setWindowTitle("Your recovery codes")
        self.setMinimumWidth(420)
        note = QLabel(
            "Keep these codes somewhere safe (a password manager or paper). Each code works ONCE and can replace "
            "the authenticator code if you lose your phone. They are shown only now."
        )
        note.setWordWrap(True)
        self.box = QPlainTextEdit("\n".join(codes))
        self.box.setReadOnly(True)
        self.box.setStyleSheet("font-family: Consolas, monospace; font-size: 13pt;")
        copy_btn = make_secondary(QPushButton("Copy"))
        copy_btn.clicked.connect(lambda: QGuiApplication.clipboard().setText(self.box.toPlainText()))
        save_btn = make_secondary(QPushButton("Save to file..."))
        save_btn.clicked.connect(self._save)
        done_btn = QPushButton("I saved them")
        done_btn.clicked.connect(self.accept)
        row = QHBoxLayout()
        row.addWidget(copy_btn)
        row.addWidget(save_btn)
        row.addStretch()
        row.addWidget(done_btn)
        layout = QVBoxLayout(self)
        layout.addWidget(note)
        layout.addWidget(self.box)
        layout.addLayout(row)

    def _save(self) -> None:
        path, _ = QFileDialog.getSaveFileName(self, "Save recovery codes", "EduMeet-recovery-codes.txt", "Text (*.txt)")
        if path:
            Path(path).write_text(self.box.toPlainText() + "\n", encoding="utf-8")


class TwoFactorSetupDialog(QDialog):
    """Scan the QR code with Google Authenticator (or Microsoft Authenticator / Authy), then confirm a code."""

    def __init__(self, api: ApiClient, parent=None):
        super().__init__(parent)
        self.api = api
        self.setWindowTitle("Turn on two-step verification")
        self.setMinimumWidth(460)
        steps = QLabel(
            "<b>1.</b> Install <b>Google Authenticator</b> (or Microsoft Authenticator / Authy) on your phone.<br>"
            "<b>2.</b> In the app tap <b>+</b> &rarr; <b>Scan a QR code</b> and scan this picture.<br>"
            "<b>3.</b> Type the 6-digit code the app shows, then press the button."
        )
        steps.setWordWrap(True)
        self.qr = QLabel("Loading QR code...")
        self.qr.setFixedSize(260, 260)
        self.qr.setScaledContents(True)
        self.qr.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.secret = QLineEdit()
        self.secret.setReadOnly(True)
        hint = QLabel("Cannot scan? Choose <i>Enter a setup key</i> in the app and type this key:")
        hint.setWordWrap(True)
        self.code = QLineEdit()
        self.code.setMaxLength(6)
        self.code.setPlaceholderText("6-digit code")
        self.code.returnPressed.connect(self._verify)
        self.btn = QPushButton("Verify and turn on")
        self.btn.clicked.connect(self._verify)
        cancel = make_secondary(QPushButton("Cancel"))
        cancel.clicked.connect(self.reject)
        row = QHBoxLayout()
        row.addWidget(self.btn)
        row.addWidget(cancel)
        layout = QVBoxLayout(self)
        layout.addWidget(steps)
        layout.addWidget(self.qr, alignment=Qt.AlignmentFlag.AlignHCenter)
        layout.addWidget(hint)
        layout.addWidget(self.secret)
        layout.addWidget(self.code)
        layout.addLayout(row)
        run_async(api.setup_2fa, self._loaded, self._error)

    def _error(self, message: str) -> None:
        QMessageBox.warning(self, "EduMeet", message)

    def _loaded(self, data: dict) -> None:
        pixmap = QPixmap()
        pixmap.loadFromData(base64.b64decode(data["qr_png_base64"]))
        self.qr.setPixmap(pixmap)
        self.secret.setText(data["secret"])

    def _verify(self) -> None:
        code = self.code.text().strip()
        if len(code) != 6:
            self._error("Enter the 6-digit code from your app.")
            return
        self.btn.setEnabled(False)

        def enabled(codes: list[str]) -> None:
            RecoveryCodesDialog(codes, self).exec()
            self.accept()

        def failed(message: str) -> None:
            self.btn.setEnabled(True)
            self._error(message)

        run_async(self.api.enable_2fa, enabled, failed, code)


class SecurityTab(QWidget):
    """Two-step verification (authenticator app) for the signed-in user."""

    def __init__(self, api: ApiClient):
        super().__init__()
        self.api = api
        title = QLabel("Two-step verification (authenticator app)")
        title.setStyleSheet("font-weight: 600; font-size: 13pt;")
        explain = QLabel(
            "Adds a 6-digit code from Google Authenticator (or Microsoft Authenticator / Authy) to your sign-in. "
            "If you ever forget your password, the same code lets you choose a new one on the sign-in screen "
            "(Forgot password tab). Recovery codes protect you if you lose your phone."
        )
        explain.setWordWrap(True)
        self.status = QLabel("Checking...")
        self.enable_btn = QPushButton("Turn on (scan QR code)")
        self.enable_btn.clicked.connect(self._enable)
        self.codes_btn = make_secondary(QPushButton("New recovery codes"))
        self.codes_btn.clicked.connect(self._new_codes)
        self.disable_btn = make_secondary(QPushButton("Turn off"), danger=True)
        self.disable_btn.clicked.connect(self._disable)
        row = QHBoxLayout()
        for button in (self.enable_btn, self.codes_btn, self.disable_btn):
            row.addWidget(button)
        row.addStretch()
        layout = QVBoxLayout(self)
        layout.addWidget(title)
        layout.addWidget(explain)
        layout.addWidget(self.status)
        layout.addLayout(row)
        layout.addStretch()
        self.refresh()

    def _error(self, message: str) -> None:
        QMessageBox.warning(self, "EduMeet", message)

    def refresh(self) -> None:
        run_async(self.api.me, self._loaded, None)

    def _loaded(self, user: dict) -> None:
        on = bool(user.get("totp_enabled"))
        self.status.setText("Status:  <b>ON</b> - a code is required when you sign in." if on else "Status:  <b>OFF</b>")
        self.enable_btn.setVisible(not on)
        self.codes_btn.setVisible(on)
        self.disable_btn.setVisible(on)

    def _enable(self) -> None:
        TwoFactorSetupDialog(self.api, self).exec()
        self.refresh()

    def _disable(self) -> None:
        dialog = PasswordCodeDialog(
            "Turn off two-step verification", "Enter your password and a current code (or a recovery code).", self
        )
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        run_async(
            self.api.disable_2fa,
            lambda _r: (QMessageBox.information(self, "EduMeet", "Two-step verification is off."), self.refresh()),
            self._error, dialog.password.text(), dialog.code.text(),
        )

    def _new_codes(self) -> None:
        dialog = PasswordCodeDialog(
            "New recovery codes", "Old recovery codes stop working. Enter your password and a current code.", self
        )
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        run_async(
            self.api.new_recovery_codes,
            lambda codes: RecoveryCodesDialog(codes, self).exec(),
            self._error, dialog.password.text(), dialog.code.text(),
        )


class MonitorTab(QWidget):
    """Administrators: who is online, security log, suspicious sign-ups and blocked e-mail domains."""

    KINDS = [
        ("All events", ""), ("Sign-in OK", "login_ok"), ("Wrong password", "login_fail"),
        ("Wrong authenticator code", "login_fail_otp"), ("Sign-in refused (already signed in)", "login_blocked"),
        ("Account locked", "account_locked"), ("Sign-up OK", "register_ok"), ("Sign-up BLOCKED", "register_blocked"),
        ("E-mail confirmed", "email_verified"), ("Sign-in before e-mail confirmed", "login_unverified"),
        ("Password reset", "password_reset_ok"), ("2FA turned on", "2fa_enabled"), ("2FA turned off", "2fa_disabled"),
        ("Same account joined a meeting twice", "join_duplicate"), ("Admin: user changed", "admin_user_updated"),
        ("Admin: signed a user out", "admin_session_revoked"), ("Sign-out", "logout"),
    ]

    def __init__(self, api: ApiClient):
        super().__init__()
        self.api = api
        self.sessions: list[dict] = []
        self.events: list[dict] = []
        self.domains: list[dict] = []

        # --- overview
        self.overview = QLabel("Loading...")
        self.overview.setTextFormat(Qt.TextFormat.RichText)
        self.overview.setWordWrap(True)
        overview_page = QWidget()
        box = QVBoxLayout(overview_page)
        box.addWidget(self.overview)
        box.addStretch()

        # --- online now
        self.online_table = make_table(["Name", "Email", "Role", "Computer", "Signed in", "Last seen"])
        kick = make_secondary(QPushButton("Sign out selected user"), danger=True)
        kick.clicked.connect(self._revoke)
        online_page = QWidget()
        box = QVBoxLayout(online_page)
        box.addWidget(self.online_table)
        box.addWidget(kick, alignment=Qt.AlignmentFlag.AlignLeft)

        # --- security log
        self.kind = QComboBox()
        for label, value in self.KINDS:
            self.kind.addItem(label, value)
        self.kind.currentIndexChanged.connect(self._load_events)
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search email, IP, computer or details - press Enter")
        self.search.returnPressed.connect(self._load_events)
        export = make_secondary(QPushButton("Export CSV..."))
        export.clicked.connect(self._export)
        self.log_table = make_table(["Time", "Event", "Email", "IP address", "Computer", "Details"])
        log_page = QWidget()
        box = QVBoxLayout(log_page)
        row = QHBoxLayout()
        row.addWidget(self.kind)
        row.addWidget(self.search, 1)
        row.addWidget(export)
        box.addLayout(row)
        box.addWidget(self.log_table)

        # --- suspicious
        self.sus_table = make_table(["What was seen", "Computer / address", "Count", "Accounts"])
        sus_page = QWidget()
        box = QVBoxLayout(sus_page)
        note = QLabel("Possible cases of one person using several accounts. Review them; nothing is blocked automatically.")
        note.setObjectName("muted")
        box.addWidget(note)
        box.addWidget(self.sus_table)

        # --- blocked domains
        self.domain_table = make_table(["Blocked e-mail domain", "Added"])
        self.domain_edit = QLineEdit()
        self.domain_edit.setPlaceholderText("example-temp-mail.com")
        self.domain_edit.returnPressed.connect(self._add_domain)
        add = QPushButton("Block domain")
        add.clicked.connect(self._add_domain)
        remove = make_secondary(QPushButton("Unblock selected"))
        remove.clicked.connect(self._remove_domain)
        domain_page = QWidget()
        box = QVBoxLayout(domain_page)
        note = QLabel(
            "These are in addition to the built-in list of temporary-mail domains. Sub-domains are blocked too."
        )
        note.setObjectName("muted")
        note.setWordWrap(True)
        box.addWidget(note)
        box.addWidget(self.domain_table)
        row = QHBoxLayout()
        row.addWidget(self.domain_edit, 1)
        row.addWidget(add)
        row.addWidget(remove)
        box.addLayout(row)

        self.tabs = QTabWidget()
        for page, title in ((overview_page, "Overview"), (online_page, "Online now"), (log_page, "Security log"),
                            (sus_page, "Suspicious"), (domain_page, "Blocked domains")):
            self.tabs.addTab(page, title)
        layout = QVBoxLayout(self)
        layout.addWidget(self.tabs)

        self.timer = QTimer(self)
        self.timer.timeout.connect(self._auto)
        self.timer.start(15_000)
        self.refresh_all()

    # ---------- loading ----------
    def _error(self, message: str) -> None:
        QMessageBox.warning(self, "EduMeet", message)

    def _auto(self) -> None:
        if self.isVisible():
            self.refresh_all()

    def refresh_all(self) -> None:
        run_async(self.api.admin_stats, self._show_stats, None)
        run_async(self.api.admin_sessions, self._show_sessions, None)
        self._load_events()
        run_async(self.api.admin_suspicious, self._show_suspicious, None)
        run_async(self.api.admin_domains, self._show_domains, None)

    def _show_stats(self, s: dict) -> None:
        self.overview.setText(
            "<table cellpadding='8'>"
            f"<tr><td><b>Registered users</b></td><td>{s['users']}</td></tr>"
            f"<tr><td><b>Online now</b></td><td>{s['online']}</td></tr>"
            f"<tr><td><b>E-mail not confirmed yet</b></td><td>{s['unverified']}</td></tr>"
            f"<tr><td><b>Failed sign-ins (last 24 h)</b></td><td>{s['failed_logins_24h']}</td></tr>"
            f"<tr><td><b>Blocked sign-ups (last 24 h)</b></td><td>{s['blocked_signups_24h']}</td></tr>"
            f"<tr><td><b>Accounts locked right now</b></td><td>{s['locked_accounts']}</td></tr></table>"
            "<p style='color:#64748b'>This page refreshes every 15 seconds. Details are in the other tabs.</p>"
        )

    def _show_sessions(self, rows: list[dict]) -> None:
        self.sessions = rows
        fill_table(self.online_table, [
            [r["name"], r["email"], r["role"], r["device"], fmt_dt(r["created_at"]), fmt_dt(r["last_seen"])] for r in rows
        ])

    def _load_events(self, *_args) -> None:
        run_async(self.api.admin_events, self._show_events, None, self.kind.currentData() or "", self.search.text().strip())

    def _show_events(self, rows: list[dict]) -> None:
        self.events = rows
        fill_table(self.log_table, [
            [fmt_dt(r["created_at"]), r["kind"], r["email"], r["ip"], r["device"], r["detail"]] for r in rows
        ])

    def _show_suspicious(self, rows: list[dict]) -> None:
        fill_table(self.sus_table, [[r["kind"], r["key"], r["count"], r["accounts"]] for r in rows])

    def _show_domains(self, rows: list[dict]) -> None:
        self.domains = rows
        fill_table(self.domain_table, [[r["domain"], fmt_dt(r["created_at"])] for r in rows])

    # ---------- actions ----------
    def _revoke(self) -> None:
        row = self.online_table.currentRow()
        if row < 0 or row >= len(self.sessions):
            return
        s = self.sessions[row]
        if QMessageBox.question(self, "Sign out", f"Sign {s['email']} out now?") != QMessageBox.StandardButton.Yes:
            return
        run_async(self.api.admin_revoke_session, lambda _r: self.refresh_all(), self._error, s["id"])

    def _add_domain(self) -> None:
        domain = self.domain_edit.text().strip()
        if not domain:
            return
        run_async(self.api.admin_add_domain, lambda _r: (self.domain_edit.clear(), self.refresh_all()), self._error, domain)

    def _remove_domain(self) -> None:
        row = self.domain_table.currentRow()
        if 0 <= row < len(self.domains):
            run_async(self.api.admin_delete_domain, lambda _r: self.refresh_all(), self._error, self.domains[row]["id"])

    def _export(self) -> None:
        path, _ = QFileDialog.getSaveFileName(self, "Export security log", "EduMeet-security-log.csv", "CSV (*.csv)")
        if not path:
            return
        with open(path, "w", newline="", encoding="utf-8-sig") as handle:
            writer = csv.writer(handle)
            writer.writerow(["time (UTC)", "event", "email", "ip", "computer", "details"])
            for r in self.events:
                writer.writerow([r["created_at"], r["kind"], r["email"], r["ip"], r["device"], r["detail"]])
        QMessageBox.information(self, "EduMeet", f"Saved {len(self.events)} events.")


class MeetingsTab(QWidget):
    def __init__(self, api: ApiClient, open_room):
        super().__init__()
        self.api, self.open_room, self.meetings = api, open_room, []
        self.last_cam, self.last_mic = True, True

        self.mine_cb = QCheckBox("Only meetings I host")
        self.mine_cb.toggled.connect(self.refresh)
        self.new_btn = QPushButton("New meeting")
        self.new_btn.setVisible(api.can_host)
        self.new_btn.clicked.connect(self._new)
        self.join_btn = QPushButton("Join")
        self.join_btn.clicked.connect(self._join_selected)
        code_btn = make_secondary(QPushButton("Join by code..."))
        code_btn.clicked.connect(self._join_by_code)
        self.copy_btn = make_secondary(QPushButton("Copy code"))
        self.copy_btn.clicked.connect(self._copy_code)
        self.files_btn = make_secondary(QPushButton("Files..."))
        self.files_btn.clicked.connect(self._files)
        self.att_btn = make_secondary(QPushButton("Attendance..."))
        self.att_btn.clicked.connect(self._attendance)
        self.end_btn = make_secondary(QPushButton("End meeting"), danger=True)
        self.end_btn.clicked.connect(self._end)
        refresh_btn = make_secondary(QPushButton("Refresh"))
        refresh_btn.clicked.connect(self.refresh)

        self.table = make_table(["Title", "Code", "Host", "Status", "Scheduled"])
        self.table.itemSelectionChanged.connect(self._update_buttons)
        self.table.cellDoubleClicked.connect(lambda *_: self._join_selected())

        top = QHBoxLayout()
        for w in (self.new_btn, code_btn):
            top.addWidget(w)
        top.addStretch()
        top.addWidget(self.mine_cb)
        top.addWidget(refresh_btn)
        bottom = QHBoxLayout()
        for w in (self.join_btn, self.copy_btn, self.files_btn, self.att_btn, self.end_btn):
            bottom.addWidget(w)
        bottom.addStretch()

        layout = QVBoxLayout(self)
        layout.addLayout(top)
        layout.addWidget(self.table)
        layout.addLayout(bottom)

        self._update_buttons()
        self.refresh()
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.refresh)
        self.timer.start(30_000)

    # ---------- helpers ----------
    def _selected(self) -> dict | None:
        row = self.table.currentRow()
        return self.meetings[row] if 0 <= row < len(self.meetings) else None

    def _update_buttons(self) -> None:
        m = self._selected()
        has = m is not None
        self.join_btn.setEnabled(has and m["status"] != "ended")
        self.copy_btn.setEnabled(has)
        self.files_btn.setEnabled(has)
        self.att_btn.setEnabled(bool(has and (self.api.is_admin or m["host_id"] == self.api.user["id"])))
        can_end = has and m["status"] != "ended" and (self.api.is_admin or m["host_id"] == self.api.user["id"])
        self.end_btn.setEnabled(bool(can_end))

    def _error(self, message: str) -> None:
        QMessageBox.warning(self, "EduMeet", message)

    # ---------- actions ----------
    def refresh(self, *_args) -> None:
        run_async(self.api.list_meetings, self._loaded, None, self.mine_cb.isChecked())

    def _loaded(self, meetings: list[dict]) -> None:
        keep = (self._selected() or {}).get("code")
        self.meetings = meetings
        fill_table(self.table, [
            [m["title"], m["code"], m["host_name"], m["status"], fmt_dt(m["scheduled_at"])]
            for m in meetings
        ])
        for i, m in enumerate(meetings):
            if m["code"] == keep:
                self.table.selectRow(i)
        self._update_buttons()

    def _new(self) -> None:
        dialog = NewMeetingDialog(self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        title, desc, when = dialog.payload()

        def created(meeting: dict) -> None:
            self.refresh()
            QMessageBox.information(self, "Meeting created",
                                    f"Share this code with participants:\n\n{meeting['code']}")
            self._enter(meeting)

        run_async(self.api.create_meeting, created, self._error, title, desc, when)

    def _enter(self, meeting: dict) -> None:
        dialog = JoinOptionsDialog(meeting, self.last_cam, self.last_mic, self, api=self.api)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.last_cam, self.last_mic = dialog.camera.isChecked(), dialog.microphone.isChecked()
            self.open_room(meeting, self.last_cam, self.last_mic)

    def _join_code(self, code: str) -> None:
        def got(meeting: dict) -> None:
            if meeting["status"] == "ended":
                self._error("This meeting has ended.")
            else:
                self._enter(meeting)

        run_async(self.api.get_meeting, got, self._error, code)

    def _join_selected(self) -> None:
        m = self._selected()
        if m and m["status"] != "ended":
            self._join_code(m["code"])

    def _join_by_code(self) -> None:
        code, ok = QInputDialog.getText(self, "Join by code", "Meeting code (e.g. abc-defg-hij):")
        if ok and code.strip():
            self._join_code(code.strip())

    def _copy_code(self) -> None:
        m = self._selected()
        if m:
            QGuiApplication.clipboard().setText(m["code"])

    def _files(self) -> None:
        m = self._selected()
        if m:
            FilesDialog(self.api, m, self).exec()

    def _attendance(self) -> None:
        m = self._selected()
        if m:
            AttendanceDialog(self.api, m, self).exec()

    def _end(self) -> None:
        m = self._selected()
        if not m:
            return
        if QMessageBox.question(self, "End meeting", f"End '{m['title']}' for everyone?") \
                != QMessageBox.StandardButton.Yes:
            return
        run_async(self.api.end_meeting, lambda _m: self.refresh(), self._error, m["code"])


class UsersTab(QWidget):
    """Administrators only: create users, change roles, deactivate accounts."""

    def __init__(self, api: ApiClient):
        super().__init__()
        self.api, self.users = api, []
        add_btn = QPushButton("Add user")
        add_btn.clicked.connect(self._add)
        self.edit_btn = make_secondary(QPushButton("Edit selected"))
        self.edit_btn.clicked.connect(self._edit)
        refresh_btn = make_secondary(QPushButton("Refresh"))
        refresh_btn.clicked.connect(self.refresh)

        self.table = make_table(["Name", "Email", "Role", "Active", "Created"])
        self.table.itemSelectionChanged.connect(lambda: self.edit_btn.setEnabled(self.table.currentRow() >= 0))
        self.table.cellDoubleClicked.connect(lambda *_: self._edit())
        self.edit_btn.setEnabled(False)

        row = QHBoxLayout()
        row.addWidget(add_btn)
        row.addWidget(self.edit_btn)
        row.addStretch()
        row.addWidget(refresh_btn)
        layout = QVBoxLayout(self)
        layout.addLayout(row)
        layout.addWidget(self.table)
        self.refresh()

    def _error(self, message: str) -> None:
        QMessageBox.warning(self, "EduMeet", message)

    def refresh(self, *_args) -> None:
        run_async(self.api.list_users, self._loaded, self._error)

    def _loaded(self, users: list[dict]) -> None:
        self.users = users
        fill_table(self.table, [
            [u["full_name"], u["email"], u["role"], "yes" if u["is_active"] else "no", fmt_dt(u["created_at"])]
            for u in users
        ])

    def _add(self) -> None:
        dialog = UserDialog(parent=self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        v = dialog.values()
        run_async(self.api.create_user, lambda _u: self.refresh(), self._error,
                  v["email"], v["full_name"], v["password"], v["role"])

    def _edit(self) -> None:
        row = self.table.currentRow()
        if row < 0:
            return
        user = self.users[row]
        dialog = UserDialog(user, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        fields = {k: v for k, v in dialog.values().items() if v not in ("", None)}
        run_async(self.api.update_user, lambda _u: self.refresh(), self._error, user["id"], **fields)


# =============================================================================
# Main window
# =============================================================================
class Dashboard(QMainWindow):
    logout_requested = Signal()

    def __init__(self, api: ApiClient):
        super().__init__()
        self.api = api
        self._rooms: list[RoomWindow] = []
        self._signed_out = False
        self.setWindowTitle("EduMeet")
        self.resize(940, 600)

        user = api.user
        header = QHBoxLayout()
        brand = QLabel("EduMeet")
        brand.setObjectName("title")
        badge = QLabel(f"{user['full_name']}  |  {user['role']}")
        badge.setObjectName("userBadge")
        test_btn = make_secondary(QPushButton("Test mic & camera"))
        test_btn.clicked.connect(self.open_device_test)
        logout = make_secondary(QPushButton("Sign out"))
        logout.clicked.connect(self._sign_out)
        header.addWidget(brand)
        header.addStretch()
        header.addWidget(test_btn)
        header.addWidget(badge)
        header.addWidget(logout)

        tabs = QTabWidget()
        tabs.addTab(MeetingsTab(api, self.open_room), "Meetings")
        tabs.addTab(SecurityTab(api), "Security")
        if api.is_admin:
            tabs.addTab(UsersTab(api), "Users (admin)")
            tabs.addTab(MonitorTab(api), "Monitor")

        root = QWidget()
        layout = QVBoxLayout(root)
        layout.addLayout(header)
        layout.addWidget(tabs)
        self.setCentralWidget(root)

        # Tell the server this app is still open (keeps "one sign-in per account" accurate)
        self.heartbeat_timer = QTimer(self)
        self.heartbeat_timer.timeout.connect(self._heartbeat)
        self.heartbeat_timer.start(30_000)

    def _sign_out(self) -> None:
        self._signed_out = True
        self.heartbeat_timer.stop()
        run_async(self.api.logout, lambda _r: self.logout_requested.emit(), lambda _m: self.logout_requested.emit())

    def _heartbeat(self) -> None:
        run_async(self.api.heartbeat, self._heartbeat_result, None)

    def _heartbeat_result(self, alive: bool) -> None:
        if alive or self._signed_out:
            return
        self._signed_out = True
        self.heartbeat_timer.stop()
        QMessageBox.warning(
            self, "EduMeet",
            "Your session has ended. This account was signed in on another device, or you were signed out. "
            "Please sign in again.",
        )
        self.logout_requested.emit()

    def open_device_test(self) -> None:
        DeviceTestDialog(self.api, self).exec()

    def open_room(self, meeting: dict, cam: bool = True, mic: bool = True) -> None:
        room = RoomWindow(self.api, meeting, cam, mic)
        room.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self._rooms.append(room)
        room.destroyed.connect(lambda *_: self._rooms.remove(room) if room in self._rooms else None)
        room.show()

    def closeEvent(self, event) -> None:  # noqa: N802
        if not self._signed_out:  # frees the account for the next sign-in right away
            try:
                self.api.logout(timeout=3)
            except Exception:  # noqa: BLE001
                pass
            self._signed_out = True
        for room in list(self._rooms):
            room.close()
        super().closeEvent(event)
