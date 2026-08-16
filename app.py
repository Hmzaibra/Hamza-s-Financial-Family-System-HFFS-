"""Application factory and CLI.

    flask --app app run --debug          development on http://localhost:5000
    flask --app app migrate              apply outstanding migrations
    flask --app app create-admin         create the first admin, interactively
    flask --app app fetch-rates          refresh cached exchange rates (cron)

Deployment (gunicorn behind `tailscale serve`) is Phase 4 and lives entirely
outside this file — no hostname, port or certificate handling belongs here.
"""

from __future__ import annotations

import secrets
import sys
from zoneinfo import ZoneInfo, available_timezones

import click
from flask import Flask, g, render_template
from werkzeug.security import generate_password_hash

import csrf
import db
import transactions
from config import Config
from money import flag, format_minor, symbol


def create_app(config_object=Config) -> Flask:
    app = Flask(__name__)
    app.config.from_object(config_object)

    if not app.config["SECRET_KEY"]:
        # An ephemeral key means every restart silently logs everyone out, which
        # is confusing rather than insecure — so say so loudly instead.
        app.config["SECRET_KEY"] = secrets.token_hex(32)
        app.logger.warning(
            "SECRET_KEY is not set. Using a random key for this process only; "
            "sessions will not survive a restart. Copy .env.example to .env and "
            "set one."
        )

    app.config["UPLOAD_DIR"].mkdir(parents=True, exist_ok=True)

    db.init_app(app)
    csrf.init_app(app)

    from blueprints import auth, dashboard, entry, ledger, reference

    app.register_blueprint(auth.bp)
    app.register_blueprint(entry.bp)
    app.register_blueprint(dashboard.bp)
    app.register_blueprint(ledger.bp)
    app.register_blueprint(reference.bp)

    app.cli.add_command(create_admin_command)
    app.cli.add_command(fetch_rates_command)

    # Templates format money through the same integer-only helpers the rest of
    # the app uses. No Jinja filter anywhere is allowed to do its own division.
    app.jinja_env.filters["money"] = format_minor
    app.jinja_env.filters["currency_symbol"] = symbol
    app.jinja_env.filters["currency_flag"] = flag
    app.jinja_env.filters["account_emoji"] = transactions.account_emoji

    @app.context_processor
    def inject_globals():
        # base_currency() needs a database, which the login page does not have a
        # session for but does have a request context for. Guarded so an error
        # page rendered before the connection exists cannot 500 in the layout.
        try:
            code = db.base_currency() if g.get("user") else "EGP"
        except Exception:
            code = "EGP"
        return {"current_user": g.get("user"), "base_currency_code": code}

    @app.errorhandler(400)
    def bad_request(exc):
        return render_template("400.html", message=getattr(exc, "description", "")), 400

    @app.errorhandler(404)
    def not_found(_):
        return render_template("404.html"), 404

    @app.after_request
    def security_headers(response):
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        # No CDNs anywhere (spec section 1), so this can be strict from day one.
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; img-src 'self' data: blob:; "
            "style-src 'self'; script-src 'self'; font-src 'self'; "
            "form-action 'self'; frame-ancestors 'none'; base-uri 'self'",
        )
        return response

    return app


# --------------------------------------------------------------------- CLI


@click.command("create-admin")
@click.option("--username", prompt=True)
@click.option("--display-name", prompt="Display name")
@click.option("--timezone", "tz", default=None, help="IANA name, e.g. Africa/Cairo.")
@click.password_option("--password", prompt=True, confirmation_prompt=True)
def create_admin_command(username: str, display_name: str, tz: str | None, password: str) -> None:
    """Create an admin user.

    Interactive by design: a password typed at a prompt never lands in a
    migration, a shell history entry or a file that could be committed.
    """
    from flask import current_app

    username = username.strip()
    display_name = display_name.strip() or username
    tz = (tz or current_app.config["DEFAULT_TIMEZONE"]).strip()

    if not username:
        click.echo("Username cannot be empty.", err=True)
        sys.exit(1)
    if len(password) < 8:
        click.echo("Use at least 8 characters.", err=True)
        sys.exit(1)
    if tz not in available_timezones():
        click.echo(f"Unknown timezone {tz!r}. Try Africa/Cairo or Europe/Berlin.", err=True)
        sys.exit(1)
    ZoneInfo(tz)

    conn = db.connect(current_app.config["DATABASE_PATH"])
    try:
        exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'users'"
        ).fetchone()
        if not exists:
            click.echo("Run `flask --app app migrate` first.", err=True)
            sys.exit(1)

        taken = conn.execute("SELECT 1 FROM users WHERE username = ?", (username,)).fetchone()
        if taken:
            click.echo(f"User {username!r} already exists.", err=True)
            sys.exit(1)

        conn.execute(
            "INSERT INTO users (username, display_name, password_hash, role, "
            "default_shared, timezone, is_active, created_at) "
            "VALUES (?, ?, ?, 'admin', 1, ?, 1, ?)",
            (username, display_name, generate_password_hash(password), tz, db.utc_now()),
        )
        conn.commit()
    finally:
        conn.close()

    click.echo(f"Created admin {username!r} ({display_name}, {tz}).")


@click.command("fetch-rates")
@click.option("--force", is_flag=True, help="Refresh even if the cache is still fresh.")
@click.option("--max-age-days", type=int, default=None,
              help="Refresh only when the cache is at least this old. Default: FX_MAX_AGE_DAYS.")
def fetch_rates_command(force: bool, max_age_days: int | None) -> None:
    """Refresh cached exchange rates.

    Safe to run daily: without --force it does nothing until the cache is older
    than the configured window, which is how a daily cron entry produces weekly
    rates. A failure here is reported and shrugged off — the entry form works
    with stale rates, and works with none at all.
    """
    import fx
    from flask import current_app

    base = db.base_currency()
    window = max_age_days if max_age_days is not None else current_app.config["FX_MAX_AGE_DAYS"]

    try:
        written, message = fx.refresh(
            base, current_app.config["FX_RATES_URL"], window, force=force
        )
    except fx.RateError as exc:
        # Not sys.exit(1): a cron job that emails on every failed DNS lookup gets
        # filtered into a folder nobody reads within a month.
        click.echo(f"rates not refreshed — {exc}", err=True)
        return

    if written:
        fx.clear_unused(base)
    click.echo(message)


app = create_app()


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=True)
