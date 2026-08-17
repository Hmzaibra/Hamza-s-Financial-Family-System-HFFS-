"""Application factory and CLI.

    flask --app app run --debug          development on http://localhost:5000
    flask --app app migrate              apply outstanding migrations
    flask --app app create-admin         create the first admin, interactively
    flask --app app fetch-rates          refresh cached exchange rates (cron)
    flask --app app check-limits         warn about budgets over their mark (cron)
    flask --app app telegram-chats       who has messaged the bot, with chat ids
    flask --app app sweep-uploads        unlink receipt files whose rows are gone

Every request is refused with a plain page while the database is behind the
migrations on disk. `git pull` brings a new one and the dev server reloads on
the file change; the database does not reload with it, and the failure that
produces is a traceback naming a missing table rather than the command that
creates it.

The last three are Phase 3 and 2. `check-limits` is a cron command and not a
request for the reason invariant 7 exists: it is the only place besides
`fetch-rates` that opens a socket, and neither may ever sit between someone and
the entry form.

Deployment (gunicorn behind `tailscale serve`) is Phase 4 and lives entirely
outside this file — no hostname, port or certificate handling belongs here.
"""

from __future__ import annotations

import secrets
import sys
from pathlib import Path
from zoneinfo import ZoneInfo, available_timezones

import click
from flask import Flask, g, render_template, request
from werkzeug.security import generate_password_hash

import csrf
import db
import transactions
from config import Config
from money import flag, format_minor, symbol


def _pending_migrations(app) -> list[str]:
    """Migration files on disk that `app`'s database has not run.

    Never creates the database as a side effect of asking: a missing file means
    a fresh install, and every migration is outstanding by definition.
    """
    import sqlite3

    from migrate import outstanding

    path = Path(app.config["DATABASE_PATH"])
    if not path.exists():
        return [p.name for p in sorted(Path(app.config["MIGRATIONS_DIR"]).glob("*.sql"))]

    conn = sqlite3.connect(str(path))
    try:
        return outstanding(conn, Path(app.config["MIGRATIONS_DIR"]))
    except Exception:
        # A database we cannot read is a different problem with its own error,
        # and blocking every request behind a guess about it would hide that.
        return []
    finally:
        conn.close()


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

    # ---- the schema has to match the code that is about to query it --------
    #
    # `git pull` brings a new migration and the dev server reloads on the file
    # change; the database does not reload with it. What that used to look like
    # was a 500 deep inside whichever view first touched the new table — a
    # traceback, not a sentence, and one that names a table rather than the
    # command that creates it.
    #
    # Checked once at boot so the healthy path costs nothing, and re-checked on
    # each request only while it is failing, so `flask migrate` in another
    # window fixes it without a restart.
    pending = {"files": _pending_migrations(app)}
    if pending["files"]:
        app.logger.error(
            "The database is behind the code — %s not applied. "
            "Run `flask --app app migrate`.", ", ".join(pending["files"]))

    @app.before_request
    def schema_matches_code():
        if not pending["files"] or request.endpoint == "static":
            return None
        pending["files"] = _pending_migrations(app)
        if not pending["files"]:
            return None
        return render_template("503.html", pending=pending["files"]), 503

    from blueprints import (
        auth, dashboard, entry, ledger, myaccounts, receipts, reference,
    )

    app.register_blueprint(auth.bp)
    app.register_blueprint(entry.bp)
    app.register_blueprint(dashboard.bp)
    app.register_blueprint(ledger.bp)
    app.register_blueprint(myaccounts.bp)
    app.register_blueprint(receipts.bp)
    app.register_blueprint(reference.bp)

    app.cli.add_command(create_admin_command)
    app.cli.add_command(fetch_rates_command)
    app.cli.add_command(check_limits_command)
    app.cli.add_command(telegram_chats_command)
    app.cli.add_command(sweep_uploads_command)

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

    @app.errorhandler(413)
    def too_large(_):
        # MAX_CONTENT_LENGTH is enforced by Werkzeug before any view runs, so a
        # 24MB photo never reaches receipts.py to be told about in a sentence.
        # Without this handler it is a bare Werkzeug page, which is a strange
        # thing to meet after tapping a camera button.
        megabytes = app.config["MAX_CONTENT_LENGTH"] // (1024 * 1024)
        return render_template(
            "400.html",
            message=f"That photo is larger than {megabytes}MB. Take it again at a "
                    f"lower resolution, or crop it to the receipt.",
        ), 413

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



@click.command("check-limits")
@click.option("--dry-run", is_flag=True, help="Show what would be sent, send nothing.")
@click.option("--date", "on", default=None, help="Evaluate as if it were this day (YYYY-MM-DD).")
def check_limits_command(dry_run: bool, on: str | None) -> None:
    """Warn about budgets that have crossed their mark.

    A cron command, not a request. Invariant 7 says nothing hits the network
    while someone is waiting for a page, and a budget warning is the least
    urgent thing in this app — it can wait for the top of the hour.

        0 * * * * cd /home/pi/expenses && .venv/bin/flask --app app check-limits

    Hourly is generous: `limit_alerts` has a UNIQUE on (limit, period, threshold)
    so each warning goes out exactly once per period however often this runs.
    Nothing is recorded unless a message actually left, so a flat network or a
    bad token costs a retry rather than a warning that is silently owed forever.

    --dry-run is the one to use while setting the cron entry up. It reads
    everything, decides everything, and sends nothing.
    """
    from datetime import date as date_type

    import limits as budgets
    from flask import current_app

    if on:
        try:
            day = date_type.fromisoformat(on)
        except ValueError:
            click.echo("--date wants YYYY-MM-DD.", err=True)
            sys.exit(1)
    else:
        # No session here, so no user timezone. The household's default is the
        # honest stand-in: a sweep is about the family's calendar, not one
        # person's, and being an hour out either side of midnight costs nothing
        # when the same run happens again next hour.
        day = db.today_for(current_app.config["DEFAULT_TIMEZONE"])

    token = current_app.config["TELEGRAM_BOT_TOKEN"]
    if not token and not dry_run:
        click.echo(
            "TELEGRAM_BOT_TOKEN is not set — nothing can be sent. "
            "Add it to .env, or run with --dry-run to see what would go out.",
            err=True,
        )
        sys.exit(1)

    for line in budgets.sweep(day, token, dry_run=dry_run):
        click.echo(line)


@click.command("telegram-chats")
def telegram_chats_command() -> None:
    """List who has messaged the bot lately, with the chat id to paste into Setup.

    Telegram will not let a bot write to someone who has never written to it, so
    there is no way around the person sending a message first. This turns that
    message into the number Setup asks for.
    """
    import telegram
    from flask import current_app

    token = current_app.config["TELEGRAM_BOT_TOKEN"]
    if not token:
        click.echo("TELEGRAM_BOT_TOKEN is not set. Add it to .env first.", err=True)
        sys.exit(1)

    try:
        chats = telegram.recent_chats(token)
    except telegram.TelegramError as exc:
        click.echo(f"could not ask telegram — {exc}", err=True)
        sys.exit(1)

    if not chats:
        click.echo(
            "Nobody has messaged the bot in the last day or so. Ask each person to "
            "open it and send anything, then run this again."
        )
        return

    for chat in chats:
        click.echo(f"  {chat['chat_id']:>16}  {chat['name']}")
    click.echo("\nPaste the number into Setup -> People for the matching person.")


@click.command("sweep-uploads")
def sweep_uploads_command() -> None:
    """Unlink receipt files whose rows are gone.

    Deleting a photo already does this in the request. This exists for the gap
    the request cannot cover: the process dying between the database commit and
    the unlink. The debt is recorded in `orphaned_files` by a trigger, inside the
    same transaction as the delete, so nothing is ever forgotten — it can only
    be late.

        0 4 * * 0 cd /home/pi/expenses && .venv/bin/flask --app app sweep-uploads
    """
    import receipts

    gone = receipts.reap(limit=100_000)
    click.echo(f"removed {gone} orphaned file(s)" if gone else "nothing to clean up")


app = create_app()


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=True)
