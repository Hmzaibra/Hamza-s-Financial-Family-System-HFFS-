"""Authentication: login, logout, session loading, and the access decorators.

No password reset by design (spec section 3) — admin sets passwords directly.
Rate limiting is backed by the login_attempts table rather than an in-process
counter, because an in-process counter is wrong the moment gunicorn runs more
than one worker and forgets everything on restart.
"""

from __future__ import annotations

import functools
import secrets
from datetime import datetime, timedelta, timezone

from flask import (
    Blueprint, current_app, flash, g, redirect, render_template, request, session, url_for,
)
from werkzeug.security import check_password_hash, generate_password_hash

from db import UTC_FORMAT, execute, query_one, utc_now

bp = Blueprint("auth", __name__)

# Hashed once at import so an unknown username costs the same as a wrong
# password. See the comment at its use site in login().
_DUMMY_HASH = generate_password_hash(secrets.token_hex(32))


# ------------------------------------------------------------ session loading


@bp.before_app_request
def load_logged_in_user() -> None:
    """Put the current user on `g` for every request, or None."""
    user_id = session.get("user_id")
    g.user = None
    if user_id is None:
        return

    user = query_one(
        "SELECT id, username, display_name, role, default_shared, timezone, is_active "
        "FROM users WHERE id = ?",
        (user_id,),
    )
    # A deactivated account must lose access immediately, not at session expiry.
    if user is None or not user["is_active"]:
        session.clear()
        return

    g.user = user


# ---------------------------------------------------------------- decorators


def login_required(view):
    @functools.wraps(view)
    def wrapped(*args, **kwargs):
        if g.get("user") is None:
            return redirect(url_for("auth.login", next=request.full_path.rstrip("?")))
        return view(*args, **kwargs)

    return wrapped


def admin_required(view):
    @functools.wraps(view)
    def wrapped(*args, **kwargs):
        user = g.get("user")
        if user is None:
            return redirect(url_for("auth.login", next=request.full_path.rstrip("?")))
        if user["role"] != "admin":
            return render_template("403.html"), 403
        return view(*args, **kwargs)

    return wrapped


# ------------------------------------------------------------- rate limiting


def _client_ip() -> str:
    """Best-effort client address.

    Behind `tailscale serve` this is the proxy, so the per-IP ceiling is a
    coarse backstop and the per-username one does the real work. X-Forwarded-For
    is deliberately not trusted: it is client-controlled unless a proxy we
    configured is known to overwrite it, and we do not configure one here.
    """
    return request.remote_addr or "unknown"


def _window_start() -> str:
    minutes = current_app.config["LOGIN_WINDOW_MINUTES"]
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes)).strftime(UTC_FORMAT)


def _recent_failures(username: str, ip: str) -> tuple[int, int]:
    since = _window_start()
    by_user = query_one(
        "SELECT COUNT(*) AS n FROM login_attempts "
        "WHERE username = ? AND succeeded = 0 AND attempted_at >= ?",
        (username, since),
    )["n"]
    by_ip = query_one(
        "SELECT COUNT(*) AS n FROM login_attempts "
        "WHERE ip = ? AND succeeded = 0 AND attempted_at >= ?",
        (ip, since),
    )["n"]
    return by_user, by_ip


def _record_attempt(username: str, ip: str, succeeded: bool) -> None:
    execute(
        "INSERT INTO login_attempts (username, ip, attempted_at, succeeded) VALUES (?, ?, ?, ?)",
        (username, ip, utc_now(), 1 if succeeded else 0),
    )
    # Keep the table small without needing a scheduled job.
    cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).strftime(UTC_FORMAT)
    execute("DELETE FROM login_attempts WHERE attempted_at < ?", (cutoff,))


def _is_locked_out(username: str, ip: str) -> bool:
    by_user, by_ip = _recent_failures(username, ip)
    return (
        by_user >= current_app.config["LOGIN_MAX_FAILS_PER_USER"]
        or by_ip >= current_app.config["LOGIN_MAX_FAILS_PER_IP"]
    )


# --------------------------------------------------------------------- views


@bp.route("/login", methods=("GET", "POST"))
def login():
    if g.get("user") is not None:
        return redirect(url_for("entry.form"))

    if request.method == "POST":
        username = (request.form.get("username") or "").strip()
        password = request.form.get("password") or ""
        remember = bool(request.form.get("remember"))
        ip = _client_ip()

        if not username or not password:
            flash("Enter your username and password.", "error")
            return render_template("login.html", username=username), 400

        if _is_locked_out(username, ip):
            minutes = current_app.config["LOGIN_WINDOW_MINUTES"]
            flash(f"Too many attempts. Try again in {minutes} minutes.", "error")
            # Still recorded, so hammering the endpoint extends the window
            # rather than resetting it.
            _record_attempt(username, ip, succeeded=False)
            return render_template("login.html", username=username), 429

        user = query_one(
            "SELECT id, password_hash, is_active FROM users WHERE username = ?",
            (username,),
        )

        # check_password_hash runs even when the user does not exist, so a
        # missing username and a wrong password cost the same time and cannot be
        # told apart by timing. _DUMMY_HASH is a real hash of an unguessable
        # value; a malformed placeholder would fail fast and give the timing
        # difference straight back.
        stored = user["password_hash"] if user else _DUMMY_HASH
        try:
            ok = check_password_hash(stored, password)
        except Exception:
            ok = False

        if not user or not ok or not user["is_active"]:
            _record_attempt(username, ip, succeeded=False)
            flash("Wrong username or password.", "error")
            return render_template("login.html", username=username), 401

        _record_attempt(username, ip, succeeded=True)

        # New session id on privilege change, so a pre-login cookie cannot be
        # reused as a logged-in one.
        session.clear()
        session["user_id"] = user["id"]
        session.permanent = remember

        target = request.args.get("next") or url_for("entry.form")
        # Only ever redirect within this app; an absolute URL here would be an
        # open redirect.
        if not target.startswith("/") or target.startswith("//"):
            target = url_for("entry.form")
        return redirect(target)

    return render_template("login.html", username="")


@bp.get("/logout")
def logout_confirm():
    """Ask before signing out.

    A page rather than a JavaScript confirm(): the CSP forbids inline handlers,
    and this way the question survives with scripting switched off. Signing back
    in is a password on a phone keyboard, so the cost of an accidental tap is
    real enough to be worth one deliberate one.
    """
    if g.get("user") is None:
        return redirect(url_for("auth.login"))
    return render_template("logout.html")


@bp.post("/logout")
def logout():
    session.clear()
    flash("Signed out.", "ok")
    return redirect(url_for("auth.login"))
