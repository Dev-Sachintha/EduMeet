"""Consent-based remote control - the SHARER's side.

A browser page cannot move the OS mouse or press keys, so the desktop app does it with pyautogui.
Safety rules built in:
  * Nothing happens unless the person sharing clicks "Allow" in a native Windows prompt
    (one prompt per request; the web page cannot approve for them).
  * A banner stays on top of the screen while someone is in control, with a Stop button.
  * Throwing the mouse into any screen corner stops control (pyautogui fail-safe).
  * Control ends automatically when screen sharing stops, either side leaves, or the room closes.
  * Only events from the approved participant are accepted, and held keys/buttons are released on stop.
"""
import json
import re

from PySide6.QtCore import QObject, Qt, Signal, Slot
from PySide6.QtWidgets import QApplication, QHBoxLayout, QLabel, QMessageBox, QPushButton, QWidget

try:
    import pyautogui

    pyautogui.FAILSAFE = True   # mouse in a screen corner -> FailSafeException -> we stop control
    pyautogui.PAUSE = 0
    AVAILABLE = True
except Exception:  # noqa: BLE001  (not installed / no display)
    pyautogui = None
    AVAILABLE = False

_SPECIAL = {
    "Enter": "enter", "NumpadEnter": "enter", "Backspace": "backspace", "Tab": "tab", "Escape": "esc",
    "Space": "space", "Delete": "delete", "Insert": "insert", "Home": "home", "End": "end",
    "PageUp": "pageup", "PageDown": "pagedown", "ArrowLeft": "left", "ArrowRight": "right",
    "ArrowUp": "up", "ArrowDown": "down", "ShiftLeft": "shiftleft", "ShiftRight": "shiftright",
    "ControlLeft": "ctrlleft", "ControlRight": "ctrlright", "AltLeft": "altleft", "AltRight": "altright",
    "MetaLeft": "winleft", "MetaRight": "winright", "CapsLock": "capslock",
    "Minus": "-", "Equal": "=", "BracketLeft": "[", "BracketRight": "]", "Semicolon": ";",
    "Quote": "'", "Comma": ",", "Period": ".", "Slash": "/", "Backslash": "\\", "Backquote": "`",
}


def key_from_code(code: str) -> str | None:
    """Map a JavaScript KeyboardEvent.code (physical key) to a pyautogui key name."""
    if re.fullmatch(r"Key[A-Z]", code):
        return code[3].lower()
    if re.fullmatch(r"Digit[0-9]", code):
        return code[5]
    if re.fullmatch(r"Numpad[0-9]", code):
        return "num" + code[6]
    if re.fullmatch(r"F([1-9]|1[0-2])", code):
        return code.lower()
    return _SPECIAL.get(code)


class ControlBanner(QWidget):
    stop_clicked = Signal()

    def __init__(self) -> None:
        super().__init__(
            None,
            Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint,
        )
        self.setStyleSheet(
            "QWidget { background:#b45309; color:white; border-radius:8px; }"
            "QPushButton { background:#7c2d12; color:white; padding:6px 14px; border-radius:6px; border:0; }"
            "QPushButton:hover { background:#431407; }"
        )
        self.label = QLabel("")
        button = QPushButton("Stop control")
        button.clicked.connect(self.stop_clicked.emit)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(14, 8, 10, 8)
        layout.addWidget(self.label)
        layout.addWidget(button)

    def show_for(self, name: str) -> None:
        self.label.setText(f"{name} is controlling your screen")
        self.adjustSize()
        area = QApplication.primaryScreen().availableGeometry()
        self.move(area.center().x() - self.width() // 2, area.top() + 8)
        self.show()
        self.raise_()


class RemoteControlBridge(QObject):
    """Exposed to the room page through QWebChannel as `bridge`."""

    consentResult = Signal(str, bool)   # peer_id, granted
    controlStopped = Signal(str)        # peer_id - stopped by the local user (banner button / fail-safe)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._controller: str | None = None
        self._dialog: QMessageBox | None = None
        self._keys: set[str] = set()
        self._buttons: set[str] = set()
        self._banner = ControlBanner()
        self._banner.stop_clicked.connect(self._user_stop)

    # ---------- consent ----------
    @Slot(str, str)
    def askConsent(self, name: str, peer_id: str) -> None:
        if not AVAILABLE or self._controller or self._dialog:
            self.consentResult.emit(peer_id, False)
            return
        box = QMessageBox(
            QMessageBox.Icon.Warning,
            "Remote control request",
            f"{name} wants to control your mouse and keyboard while you share your screen.\n\n"
            "Only allow this if you trust them. You can stop at any time with the orange "
            "'Stop control' bar, or by throwing your mouse into a screen corner.",
            QMessageBox.StandardButton.NoButton,
        )
        allow = box.addButton("Allow", QMessageBox.ButtonRole.AcceptRole)
        deny = box.addButton("Deny", QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(deny)
        box.setEscapeButton(deny)
        box.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, True)
        box.finished.connect(lambda _r: self._answered(box, allow, name, peer_id))
        self._dialog = box
        box.show()

    def _answered(self, box: QMessageBox, allow, name: str, peer_id: str) -> None:
        granted = box.clickedButton() is allow
        self._dialog = None
        box.deleteLater()
        if granted:
            self._controller = peer_id
            self._banner.show_for(name)
        self.consentResult.emit(peer_id, granted)

    # ---------- input ----------
    @Slot(str, str)
    def inject(self, payload: str, peer_id: str) -> None:
        if not AVAILABLE or peer_id != self._controller:
            return
        try:
            event = json.loads(payload)
            self._apply(event)
        except pyautogui.FailSafeException:
            self._user_stop()
        except Exception:  # noqa: BLE001  (bad data from the network must never crash the app)
            pass

    def _apply(self, ev: dict) -> None:
        width, height = pyautogui.size()
        kind = ev.get("t")

        def position() -> tuple[float, float]:
            x = min(max(float(ev["x"]), 0.01), 0.99) * width    # stay away from the fail-safe corners
            y = min(max(float(ev["y"]), 0.01), 0.99) * height
            return x, y

        if kind == "move":
            pyautogui.moveTo(*position())
        elif kind in ("down", "up"):
            button = {0: "left", 1: "middle", 2: "right"}.get(ev.get("b"))
            if button:
                pyautogui.moveTo(*position())
                if kind == "down":
                    self._buttons.add(button)
                    pyautogui.mouseDown(button=button)
                else:
                    self._buttons.discard(button)
                    pyautogui.mouseUp(button=button)
        elif kind == "wheel":
            pyautogui.scroll(int(max(-1200, min(1200, float(ev.get("dy", 0))))))
        elif kind in ("kd", "ku"):
            key = key_from_code(str(ev.get("c", "")))
            if key:
                if kind == "kd":
                    self._keys.add(key)
                    pyautogui.keyDown(key)
                else:
                    self._keys.discard(key)
                    pyautogui.keyUp(key)

    # ---------- stopping ----------
    @Slot()
    def stopControl(self) -> None:
        """Stop silently (the page already knows)."""
        self._end()

    def _user_stop(self) -> None:
        peer = self._controller
        self._end()
        if peer:
            self.controlStopped.emit(peer)

    def _end(self) -> None:
        if AVAILABLE:
            for key in list(self._keys):
                try:
                    pyautogui.keyUp(key)
                except Exception:  # noqa: BLE001
                    pass
            for button in list(self._buttons):
                try:
                    pyautogui.mouseUp(button=button)
                except Exception:  # noqa: BLE001
                    pass
        self._keys.clear()
        self._buttons.clear()
        self._controller = None
        self._banner.hide()

    def shutdown(self) -> None:
        self._end()
        if self._dialog:
            self._dialog.close()
        self._banner.close()
