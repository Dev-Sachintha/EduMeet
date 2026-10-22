"""E-mail rules: no disposable (temporary) addresses, no fake domains, one person = one address.

* canonical_email(): name+tag@x.com, and dots in Gmail, count as the SAME address.
* check_new_email(): allow-list, disposable block-list (built-in file + admin list + .env), DNS check.
"""
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from pathlib import Path

from email_validator import EmailNotValidError, EmailUndeliverableError, caching_resolver, validate_email
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.config import settings
from backend.models import BlockedDomain, User

DATA_FILE = Path(__file__).resolve().parent / "data" / "disposable_domains.txt"
_builtin: set[str] | None = None
_dns_pool = ThreadPoolExecutor(max_workers=4)
_resolver = None
DNS_BUDGET_SECONDS = 6  # never make a sign-up wait longer than this for the DNS check


def _dns_check(email: str) -> None:
    global _resolver
    if _resolver is None:
        _resolver = caching_resolver(timeout=4)  # per-lookup limit; answers are cached
    validate_email(email, check_deliverability=True, dns_resolver=_resolver)


class EmailRejected(Exception):
    def __init__(self, reason: str, message: str):
        super().__init__(message)
        self.reason, self.message = reason, message


def builtin_domains() -> set[str]:
    global _builtin
    if _builtin is None:
        try:
            lines = DATA_FILE.read_text(encoding="utf-8").splitlines()
        except OSError:
            lines = []
        _builtin = {ln.strip().lower() for ln in lines if ln.strip() and not ln.lstrip().startswith("#")}
    return _builtin


def _csv(value: str) -> set[str]:
    return {d.strip().lower() for d in value.split(",") if d.strip()}


def domain_parents(domain: str) -> list[str]:
    """'a.b.example.com' -> ['a.b.example.com', 'b.example.com', 'example.com'] (never the bare TLD)."""
    parts = domain.lower().strip(".").split(".")
    return [".".join(parts[i:]) for i in range(len(parts) - 1)]


def is_blocked(domain: str, blocked: set[str]) -> bool:
    return any(p in blocked for p in domain_parents(domain))


def canonical_email(email: str) -> str:
    local, _, domain = email.strip().lower().rpartition("@")
    if domain == "googlemail.com":
        domain = "gmail.com"
    if settings.strip_plus_aliases:
        local = local.split("+", 1)[0]
    if domain == "gmail.com":
        local = local.replace(".", "")
    return f"{local}@{domain}"


def check_new_email(db: Session, email: str) -> tuple[str, str]:
    """Validate a NEW self-registered address. Returns (clean_email, canonical). Raises EmailRejected."""
    email = email.strip().lower()
    domain = email.rpartition("@")[2]

    allowed = _csv(settings.allowed_email_domains)
    if allowed and not is_blocked(domain, allowed):  # is_blocked() == "domain or a parent is in the set"
        raise EmailRejected("domain_not_allowed", "Only email addresses from these domains can register: " + ", ".join(sorted(allowed)))

    blocked = builtin_domains() | _csv(settings.blocked_email_domains) | {d.domain for d in db.scalars(select(BlockedDomain))}
    if is_blocked(domain, blocked):
        raise EmailRejected("disposable", "Temporary / disposable email addresses are not allowed. Please use your real school or work email.")

    if settings.check_email_dns:
        try:
            _dns_pool.submit(_dns_check, email).result(timeout=DNS_BUDGET_SECONDS)
        except FutureTimeout:
            pass  # slow DNS: do not punish honest users
        except EmailUndeliverableError:
            raise EmailRejected("fake_domain", "This email domain cannot receive email. Check the spelling or use a real address.") from None
        except EmailNotValidError as exc:
            raise EmailRejected("invalid", f"This email address is not valid: {exc}") from None
        except Exception:  # noqa: BLE001  (DNS hiccup / offline: do not block honest users)
            pass

    canonical = canonical_email(email)
    taken = db.scalar(select(User.id).where((User.email_canonical == canonical) | (User.email == email)))
    if taken is not None:
        raise EmailRejected(
            "duplicate_alias",
            "This address, or a variant of it (name+tag@gmail.com, or dots in a Gmail address), is already registered. One person can have only one account.",
        )
    return email, canonical


def backfill_canonical(db: Session) -> None:
    """Fill email_canonical for accounts created before this feature existed."""
    changed = False
    for user in db.scalars(select(User).where(User.email_canonical.is_(None))):
        user.email_canonical = canonical_email(user.email)
        changed = True
    if changed:
        db.commit()
