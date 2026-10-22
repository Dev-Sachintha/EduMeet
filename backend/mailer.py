"""Send e-mail through SMTP. Without SMTP settings the message is printed in the server window (dev mode)."""
import logging
import smtplib
import ssl
from email.message import EmailMessage

from backend.config import settings

log = logging.getLogger("edumeet.mail")
last_error = ""


def status_line() -> str:
    if settings.smtp_host:
        return (
            f"Email: SMTP {settings.smtp_host}:{settings.smtp_port} as {settings.smtp_user or '(no login)'} "
            "- test it with:  python main.py test-mail --to you@gmail.com"
        )
    return "Email: NOT configured - verification codes are only printed in this window. Fill SMTP_* in .env to send real email."


def explain_error(exc: Exception) -> str:
    if isinstance(exc, smtplib.SMTPAuthenticationError):
        return (
            "The mail server refused the login. For Gmail: turn on 2-Step Verification, create an APP PASSWORD "
            "(16 letters) and put it in SMTP_PASSWORD (not your normal password). SMTP_USER must be that same Gmail address."
        )
    if isinstance(exc, smtplib.SMTPRecipientsRefused):
        return "The mail server refused the recipient address."
    if isinstance(exc, smtplib.SMTPSenderRefused):
        return "The mail server refused the sender address. SMTP_FROM must be your own address (or leave it empty)."
    if isinstance(exc, (smtplib.SMTPConnectError, OSError)):
        return (
            f"Could not connect to {settings.smtp_host}:{settings.smtp_port}. Check SMTP_HOST / SMTP_PORT, your internet "
            f"connection, and that a firewall or antivirus is not blocking it. ({exc})"
        )
    return str(exc)


def send_mail(to: str, subject: str, body: str) -> bool:
    """True when handled (really sent, or printed in dev mode); False when SMTP is set but failed."""
    global last_error
    last_error = ""
    if not settings.smtp_host:
        print(
            f"\n===== EduMeet mail (SMTP not configured - printed here) =====\nTo: {to}\nSubject: {subject}\n\n{body}\n=====\n",
            flush=True,
        )
        return True

    message = EmailMessage()
    message["From"] = settings.smtp_from or settings.smtp_user
    message["To"] = to
    message["Subject"] = subject
    message.set_content(body)
    password = settings.smtp_password.replace(" ", "")  # Google shows app passwords in groups of four
    try:
        if settings.smtp_port == 465:  # implicit SSL
            with smtplib.SMTP_SSL(settings.smtp_host, settings.smtp_port, timeout=10, context=ssl.create_default_context()) as smtp:
                if settings.smtp_user:
                    smtp.login(settings.smtp_user, password)
                smtp.send_message(message)
        else:  # normally port 587 + STARTTLS
            with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=10) as smtp:
                if settings.smtp_starttls:
                    smtp.starttls(context=ssl.create_default_context())
                if settings.smtp_user:
                    smtp.login(settings.smtp_user, password)
                smtp.send_message(message)
        print(f"[EduMeet mail] sent to {to}", flush=True)
        return True
    except Exception as exc:  # noqa: BLE001
        last_error = explain_error(exc)
        log.error("Could not send mail to %s: %s", to, exc)
        print(f"[EduMeet mail] FAILED to send to {to}: {last_error}", flush=True)
        return False


def send_test(to: str) -> tuple[bool, str]:
    ok = send_mail(to, "EduMeet test email", "If you can read this, EduMeet can send verification codes by email.\n")
    if not settings.smtp_host:
        return False, "SMTP_HOST is empty, so nothing was sent. Fill the SMTP_* lines in .env first."
    return ok, f"Test email sent to {to}. Check the inbox (and Spam)." if ok else last_error
