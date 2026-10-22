"""Two-step verification helpers (no database needed)."""
import base64

import pyotp

from backend.security import (
    generate_recovery_codes, hash_recovery, new_totp_secret, normalize_recovery, qr_png_base64, verify_totp,
)


def test_totp_accepts_a_fresh_code_once():
    secret = new_totp_secret()
    code = pyotp.TOTP(secret).now()
    step = verify_totp(secret, code, 0)
    assert step is not None
    assert verify_totp(secret, code, step) is None            # replaying the same code is refused
    assert verify_totp(secret, "abcdef", 0) is None
    assert verify_totp(secret, "12345", 0) is None


def test_recovery_codes():
    codes = generate_recovery_codes(8)
    assert len(set(codes)) == 8
    assert normalize_recovery(codes[0].upper().replace("-", " ")) == codes[0]
    assert normalize_recovery("123456") is None                # an authenticator code is not a recovery code
    assert hash_recovery(codes[0]) != hash_recovery(codes[1])


def test_qr_is_a_png():
    png = base64.b64decode(qr_png_base64("otpauth://totp/EduMeet:a@b.c?secret=ABCDEFGH&issuer=EduMeet"))
    assert png.startswith(b"\x89PNG")
