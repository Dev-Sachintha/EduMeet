"""Application settings, loaded from environment variables and the project's .env file."""
from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=BASE_DIR / ".env", env_file_encoding="utf-8", extra="ignore"
    )

    database_url: str = "sqlite:///edumeet.db"
    secret_key: str = "dev-only-change-me"
    access_token_expire_minutes: int = 480

    first_admin_email: str = "admin@example.com"
    first_admin_password: str = "ChangeMe123!"
    first_admin_name: str = "EduMeet Admin"

    upload_dir: str = "uploads"
    max_upload_mb: int = 25
    max_recording_mb: int = 1024

    single_session_per_account: bool = True
    one_account_per_device: bool = True
    session_idle_seconds: int = 120
    max_failed_attempts: int = 5
    lockout_minutes: int = 5

    # e-mail rules
    require_email_verification: bool = True      # new accounts must enter a code sent to their address
    allowed_email_domains: str = ""              # e.g. "myschool.edu,mycompany.com" (empty = any real domain)
    blocked_email_domains: str = ""              # extra domains to refuse (comma separated)
    check_email_dns: bool = True                 # refuse domains that cannot receive mail
    strip_plus_aliases: bool = True              # name+tag@x.com counts as name@x.com
    max_accounts_per_device: int = 1             # self-registered accounts per computer (0 = unlimited)
    max_registrations_per_ip_per_day: int = 5    # 0 = unlimited
    email_code_minutes: int = 10

    # outgoing mail (leave smtp_host empty: codes are printed in the server window instead)
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_from: str = ""
    smtp_starttls: bool = True

    # AI
    anthropic_api_key: str = ""
    ai_model: str = "claude-sonnet-5-5"
    whisper_model: str = "base"
    whisper_language: str = ""
    ai_max_parallel_transcriptions: int = 2
    ai_calls_per_10_min: int = 20
    max_material_mb: int = 300

    # attendance
    attendance_grace_minutes: int = 10      # joining later than this after the start = late
    attendance_min_percent: int = 50        # present for at least this % of the session = attended
    cors_origins: str = "*"

    stun_urls: str = "stun:stun.l.google.com:19302"
    turn_url: str = ""
    turn_username: str = ""
    turn_credential: str = ""

    @property
    def upload_path(self) -> Path:
        path = Path(self.upload_dir)
        if not path.is_absolute():
            path = BASE_DIR / path
        path.mkdir(parents=True, exist_ok=True)
        return path

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @property
    def ice_servers(self) -> list[dict]:
        servers: list[dict] = []
        stun = [u.strip() for u in self.stun_urls.split(",") if u.strip()]
        if stun:
            servers.append({"urls": stun})
        turn = [u.strip() for u in self.turn_url.split(",") if u.strip()]
        if turn and self.turn_username:
            servers.append(
                {"urls": turn, "username": self.turn_username, "credential": self.turn_credential}
            )
        return servers


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
