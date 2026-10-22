"""Sign-in / registration / forgot-password window."""
from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QComboBox, QDialog, QFormLayout, QHBoxLayout, QLabel, QLineEdit, QMessageBox, QPushButton, QTabWidget,
    QVBoxLayout, QWidget,
)

from desktop.api_client import ApiClient, run_async


class VerifyEmailDialog(QDialog):
    """Type the 6-digit code that was e-mailed to the address (proves the address is real)."""

    def __init__(self, client: ApiClient, email: str, parent=None):
        super().__init__(parent)
        self.client, self.email = client, email
        self.setWindowTitle("Confirm your email")
        self.setMinimumWidth(420)
        note = QLabel(
            f"We sent a 6-digit code to <b>{email}</b>.<br>Open your email, then type the code here. "
            "It works for 10 minutes.<br><br><span style='color:#64748b'>Running the server yourself without email "
            "(SMTP) settings? The code is printed in the server window.</span>"
        )
        note.setWordWrap(True)
        self.code = QLineEdit()
        self.code.setMaxLength(6)
        self.code.setPlaceholderText("6-digit code")
        self.code.returnPressed.connect(self._verify)
        self.info = QLabel("")
        self.info.setObjectName("error")
        self.info.setWordWrap(True)
        self.verify_btn = QPushButton("Confirm")
        self.verify_btn.clicked.connect(self._verify)
        resend = QPushButton("Send a new code")
        resend.setProperty("secondary", True)
        resend.clicked.connect(self._resend)
        row = QHBoxLayout()
        row.addWidget(self.verify_btn)
        row.addWidget(resend)
        layout = QVBoxLayout(self)
        layout.addWidget(note)
        layout.addWidget(self.code)
        layout.addWidget(self.info)
        layout.addLayout(row)

    def _verify(self) -> None:
        code = self.code.text().strip()
        if len(code) != 6:
            self.info.setText("Enter the 6-digit code.")
            return
        self.verify_btn.setEnabled(False)

        def failed(message: str) -> None:
            self.verify_btn.setEnabled(True)
            self.info.setText(message)

        run_async(self.client.verify_email, lambda _user: self.accept(), failed, self.email, code)

    def _resend(self) -> None:
        run_async(
            self.client.resend_code, lambda _r: self.info.setText("A new code was sent."),
            lambda message: self.info.setText(message), self.email,
        )


class LoginWindow(QWidget):
    logged_in = Signal(object)  # emits a ready-to-use ApiClient

    def __init__(self, server_url: str):
        super().__init__()
        self.setWindowTitle("EduMeet - Sign in")
        self.setFixedWidth(460)

        title = QLabel("EduMeet")
        title.setObjectName("title")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        subtitle = QLabel("Online classes and meetings")
        subtitle.setObjectName("subtitle")
        subtitle.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self.server = QLineEdit(server_url)
        server_form = QFormLayout()
        server_form.addRow("Server", self.server)

        self.tabs = QTabWidget()
        self.tabs.addTab(self._build_login_tab(), "Sign in")
        self.tabs.addTab(self._build_register_tab(), "Create account")
        self.tabs.addTab(self._build_reset_tab(), "Forgot password")

        self.error = QLabel("")
        self.error.setObjectName("error")
        self.error.setWordWrap(True)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(28, 24, 28, 24)
        layout.setSpacing(10)
        layout.addWidget(title)
        layout.addWidget(subtitle)
        layout.addLayout(server_form)
        layout.addWidget(self.tabs)
        layout.addWidget(self.error)

    # ---------- tabs ----------
    def _build_login_tab(self) -> QWidget:
        tab = QWidget()
        self.login_email = QLineEdit()
        self.login_email.setPlaceholderText("you@school.edu")
        self.login_password = QLineEdit()
        self.login_password.setEchoMode(QLineEdit.EchoMode.Password)
        self.login_password.returnPressed.connect(self._login)
        self.login_otp = QLineEdit()
        self.login_otp.setPlaceholderText("6-digit code (or recovery code)")
        self.login_otp.returnPressed.connect(self._login)
        self.otp_label = QLabel("Authenticator")
        self.login_btn = QPushButton("Sign in")
        self.login_btn.clicked.connect(self._login)
        form = QFormLayout(tab)
        form.addRow("Email", self.login_email)
        form.addRow("Password", self.login_password)
        form.addRow(self.otp_label, self.login_otp)
        form.addRow(self.login_btn)
        self._show_otp(False)
        return tab

    def _build_register_tab(self) -> QWidget:
        tab = QWidget()
        self.reg_name = QLineEdit()
        self.reg_email = QLineEdit()
        self.reg_password = QLineEdit()
        self.reg_password.setEchoMode(QLineEdit.EchoMode.Password)
        self.reg_password.setPlaceholderText("At least 8 characters")
        self.reg_role = QComboBox()
        self.reg_role.addItems(["student", "employee"])
        note = QLabel("Teacher, lecturer and admin accounts are created by an administrator.")
        note.setObjectName("muted")
        note.setWordWrap(True)
        self.reg_btn = QPushButton("Create account")
        self.reg_btn.clicked.connect(self._register)
        form = QFormLayout(tab)
        form.addRow("Full name", self.reg_name)
        form.addRow("Email", self.reg_email)
        form.addRow("Password", self.reg_password)
        form.addRow("I am a", self.reg_role)
        form.addRow(note)
        form.addRow(self.reg_btn)
        return tab

    def _build_reset_tab(self) -> QWidget:
        tab = QWidget()
        note = QLabel(
            "Forgot your password? Enter the 6-digit code from your authenticator app (or a recovery code) "
            "to choose a new one. This works only if you turned on two-step verification in the Security "
            "tab. Otherwise ask an administrator to reset your password."
        )
        note.setObjectName("muted")
        note.setWordWrap(True)
        self.reset_email = QLineEdit()
        self.reset_code = QLineEdit()
        self.reset_code.setPlaceholderText("6-digit code or recovery code")
        self.reset_pw = QLineEdit()
        self.reset_pw.setEchoMode(QLineEdit.EchoMode.Password)
        self.reset_pw.setPlaceholderText("New password (8+ characters)")
        self.reset_pw2 = QLineEdit()
        self.reset_pw2.setEchoMode(QLineEdit.EchoMode.Password)
        self.reset_pw2.setPlaceholderText("Repeat new password")
        self.reset_btn = QPushButton("Set new password")
        self.reset_btn.clicked.connect(self._reset)
        form = QFormLayout(tab)
        form.addRow(note)
        form.addRow("Email", self.reset_email)
        form.addRow("Code", self.reset_code)
        form.addRow("New password", self.reset_pw)
        form.addRow("Repeat", self.reset_pw2)
        form.addRow(self.reset_btn)
        return tab

    # ---------- helpers ----------
    def _show_otp(self, visible: bool) -> None:
        self.otp_label.setVisible(visible)
        self.login_otp.setVisible(visible)
        if not visible:
            self.login_otp.clear()

    def _busy(self, busy: bool) -> None:
        for button in (self.login_btn, self.reg_btn, self.reset_btn):
            button.setEnabled(not busy)
        if busy:
            self.error.setText("Contacting server...")

    def _failed(self, message: str) -> None:
        self._busy(False)
        self.error.setText(message)

    def _succeeded(self, client: ApiClient) -> None:
        self._busy(False)
        self.error.setText("")
        self.logged_in.emit(client)

    # ---------- actions ----------
    def _login(self) -> None:
        email, password = self.login_email.text().strip(), self.login_password.text()
        if not email or not password:
            self.error.setText("Enter your email and password.")
            return
        otp = self.login_otp.text().strip() if self.login_otp.isVisible() else ""
        client = ApiClient(self.server.text())
        self._busy(True)
        run_async(client.login, lambda data: self._login_done(client, data), self._failed, email, password, otp)

    def _login_done(self, client: ApiClient, data: dict) -> None:
        if data.get("verification_required"):
            self._busy(False)
            self._ask_verification(client, self.login_email.text().strip())
            return
        if data.get("otp_required"):
            self._busy(False)
            self._show_otp(True)
            self.login_otp.setFocus()
            self.error.setText("Enter the 6-digit code from your authenticator app (or a recovery code).")
            return
        self._succeeded(client)

    def _register(self) -> None:
        name, email = self.reg_name.text().strip(), self.reg_email.text().strip()
        password, role = self.reg_password.text(), self.reg_role.currentText()
        if not name or not email or not password:
            self.error.setText("Fill in all fields.")
            return
        client = ApiClient(self.server.text())
        self._busy(True)
        run_async(
            client.register, lambda data: self._register_done(client, email, data), self._failed,
            email, name, password, role,
        )

    def _register_done(self, client: ApiClient, email: str, data: dict) -> None:
        if data.get("verification_required"):
            self._busy(False)
            self._ask_verification(client, email)
            return
        self._succeeded(client)

    def _ask_verification(self, client: ApiClient, email: str) -> None:
        self.error.setText("")
        if VerifyEmailDialog(client, email, self).exec() == QDialog.DialogCode.Accepted:
            self._succeeded(client)
        else:
            self.error.setText("Your email is not confirmed yet. Sign in again to get a new code.")

    def _reset(self) -> None:
        email, code = self.reset_email.text().strip(), self.reset_code.text().strip()
        pw, pw2 = self.reset_pw.text(), self.reset_pw2.text()
        if not email or not code or not pw:
            self.error.setText("Fill in your email, the code and a new password.")
            return
        if pw != pw2:
            self.error.setText("The two passwords do not match.")
            return
        client = ApiClient(self.server.text())
        self._busy(True)

        def done(_result) -> None:
            self._busy(False)
            self.error.setText("")
            QMessageBox.information(self, "EduMeet", "Your password was changed. Please sign in with the new password.")
            self.login_email.setText(email)
            self.login_password.clear()
            self.reset_code.clear()
            self.reset_pw.clear()
            self.reset_pw2.clear()
            self.tabs.setCurrentIndex(0)

        run_async(client.reset_password, done, self._failed, email, code, pw)
