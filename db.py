"""SQLite connection handling.

Every connection sets its own pragmas: SQLite defaults foreign keys OFF and the
setting is per-connection, not stored in the file. Forgetting this is how
ON DELETE CASCADE quietly stops working.
"""

from __future__ import annotations

import sqlite3
from datetime import date, datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import click
from flask import current_app, g

UTC_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


def utc_now() -> str:
    """Timestamp for created_at/updated_at columns. Always UTC (hard rule 3)."""
    return datetime.now(timezone.utc).strftime(UTC_FORMAT)


def today_for(tz_name: str) -> date:
    """The calendar date it is *for this person*, which is what occurred_on means.

    Hard rule 3 stores dates as local calendar dates. Local to whom is the part
    the spec left open: a purchase at 1am in Cairo belongs to that Cairo day even
    if the Pi is in Aachen, so the answer is the user's own timezone.
    """
    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        # A bad timezone string must not take down the entry form.
        tz = ZoneInfo("UTC")
    return datetime.now(tz).date()


def connect(path: str | Path) -> sqlite3.Connection:
    """Open a connection with the pragmas and row factory this app assumes."""
    conn = sqlite3.connect(
        str(path),
        detect_types=0,
        # Wait rather than raise if a write is briefly blocked. On a Pi with a
        # slow SD card, the default 5s is optimistic.
        timeout=15.0,
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    # Without this, a foreign-key CASCADE deletes the row but fires no delete
    # trigger — so deleting a transaction would drop its attachment rows and
    # never record the JPEGs they left on disk (migration 005). Off by default,
    # and the failure is invisible: the app keeps working, the folder keeps
    # growing.
    conn.execute("PRAGMA recursive_triggers = ON")
    # WAL lets the daily limit sweep (Phase 3) read while the entry form writes.
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA busy_timeout = 15000")
    return conn


def get_db() -> sqlite3.Connection:
    """The request-scoped connection. One per request, closed on teardown."""
    if "db" not in g:
        g.db = connect(current_app.config["DATABASE_PATH"])
    return g.db


def close_db(exc=None) -> None:
    conn = g.pop("db", None)
    if conn is not None:
        conn.close()


def query(sql: str, params: tuple | list = ()) -> list[sqlite3.Row]:
    return get_db().execute(sql, params).fetchall()


def query_one(sql: str, params: tuple | list = ()) -> sqlite3.Row | None:
    return get_db().execute(sql, params).fetchone()


def execute(sql: str, params: tuple | list = ()) -> sqlite3.Cursor:
    conn = get_db()
    cur = conn.execute(sql, params)
    conn.commit()
    return cur


def get_setting(key: str, default: str | None = None) -> str | None:
    row = query_one("SELECT value FROM settings WHERE key = ?", (key,))
    return row["value"] if row else default


def set_setting(key: str, value: str) -> None:
    execute(
        "INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value, "
        "updated_at = excluded.updated_at",
        (key, value, utc_now()),
    )


def base_currency() -> str:
    return get_setting("base_currency", "EGP") or "EGP"


# --------------------------------------------------------------------- CLI


@click.command("migrate")
def migrate_command() -> None:
    """Apply any outstanding migrations."""
    from migrate import migrate as run_migrations

    click.echo(f"Database: {current_app.config['DATABASE_PATH']}")
    conn = connect(current_app.config["DATABASE_PATH"])
    try:
        run_migrations(conn, current_app.config["MIGRATIONS_DIR"], log=click.echo)
    finally:
        conn.close()


def init_app(app) -> None:
    app.teardown_appcontext(close_db)
    app.cli.add_command(migrate_command)
