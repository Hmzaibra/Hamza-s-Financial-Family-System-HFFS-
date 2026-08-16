"""Configuration, driven entirely by environment.

Deployment concerns (hostname, TLS, port) deliberately do not appear here. The
app knows only whether its cookies should carry the Secure flag; everything else
about transport belongs to the layer in front of it (spec section 8).
"""

import os
from datetime import timedelta
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent

load_dotenv(BASE_DIR / ".env")


def _flag(name: str, default: str = "0") -> bool:
    return os.environ.get(name, default).strip().lower() in {"1", "true", "yes", "on"}


def _int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    try:
        return int(raw) if raw else default
    except ValueError:
        return default


def _path(name: str, default: str) -> Path:
    """Resolve a configured path relative to the project folder.

    Spec section 1: everything lives in one folder. An absolute path is honoured
    if given, but the default keeps app.db and uploads/ inside the tree so that
    migration to the Pi stays `rsync` plus a systemd unit.
    """
    raw = os.environ.get(name, "").strip() or default
    p = Path(raw)
    return p if p.is_absolute() else BASE_DIR / p


class Config:
    BASE_DIR = BASE_DIR

    SECRET_KEY = os.environ.get("SECRET_KEY", "")

    DATABASE_PATH = _path("DATABASE_PATH", "app.db")
    UPLOAD_DIR = _path("UPLOAD_DIR", "uploads")
    MIGRATIONS_DIR = BASE_DIR / "migrations"

    # Session cookie. HttpOnly and SameSite are not negotiable; Secure depends on
    # whether there is TLS in front of us, which only the deployment knows.
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = "Lax"
    SESSION_COOKIE_SECURE = _flag("SESSION_COOKIE_SECURE")
    PERMANENT_SESSION_LIFETIME = timedelta(days=_int("SESSION_DAYS", 30))

    DEFAULT_TIMEZONE = os.environ.get("DEFAULT_TIMEZONE", "").strip() or "Europe/Berlin"

    LOGIN_WINDOW_MINUTES = _int("LOGIN_WINDOW_MINUTES", 15)
    LOGIN_MAX_FAILS_PER_USER = _int("LOGIN_MAX_FAILS_PER_USER", 8)
    LOGIN_MAX_FAILS_PER_IP = _int("LOGIN_MAX_FAILS_PER_IP", 20)

    TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()

    # Exchange rates. Touched only by `flask fetch-rates`, never by a request —
    # see fx.py. The URL is configurable because a free provider today may not be
    # one in two years, and swapping it should not mean editing code over SSH.
    FX_RATES_URL = (
        os.environ.get("FX_RATES_URL", "").strip()
        or "https://open.er-api.com/v6/latest/{base}"
    )
    FX_MAX_AGE_DAYS = _int("FX_MAX_AGE_DAYS", 7)

    # Phase 2 will need this; declared now so the limit lives in one place.
    MAX_CONTENT_LENGTH = 24 * 1024 * 1024
