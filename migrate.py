"""Migration runner.

Numbered .sql files in migrations/, applied in order, each recorded in
schema_migrations so a re-run is a no-op. Deliberately not Alembic: this has to
stay debuggable over SSH on a Pi in two years, and the whole mechanism is the
sixty lines below.

Rules it enforces:
  - a file is applied exactly once, inside a transaction with its bookkeeping row
  - applying stops at the first failure; nothing partial is recorded
  - a file whose contents changed after being applied is reported, not silently
    re-run (edit history is a bug; add 003 instead)
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path

MIGRATION_RE = re.compile(r"^(\d+)_.+\.sql$")

BOOTSTRAP = """
CREATE TABLE IF NOT EXISTS schema_migrations (
  version    INTEGER PRIMARY KEY,
  filename   TEXT NOT NULL,
  checksum   TEXT NOT NULL,
  applied_at TEXT NOT NULL
);
"""


@dataclass(frozen=True)
class Migration:
    version: int
    path: Path

    @property
    def filename(self) -> str:
        return self.path.name

    def sql(self) -> str:
        return self.path.read_text(encoding="utf-8")

    def checksum(self) -> str:
        return hashlib.sha256(self.path.read_bytes()).hexdigest()[:16]


def discover(migrations_dir: Path) -> list[Migration]:
    found: list[Migration] = []
    for path in sorted(migrations_dir.glob("*.sql")):
        match = MIGRATION_RE.match(path.name)
        if not match:
            raise RuntimeError(
                f"migration filename must be NNN_description.sql, got {path.name!r}"
            )
        found.append(Migration(version=int(match.group(1)), path=path))

    versions = [m.version for m in found]
    duplicates = {v for v in versions if versions.count(v) > 1}
    if duplicates:
        raise RuntimeError(f"duplicate migration version(s): {sorted(duplicates)}")

    return sorted(found, key=lambda m: m.version)


def applied(conn: sqlite3.Connection) -> dict[int, str]:
    conn.executescript(BOOTSTRAP)
    rows = conn.execute("SELECT version, checksum FROM schema_migrations").fetchall()
    return {row[0]: row[1] for row in rows}


def migrate(conn: sqlite3.Connection, migrations_dir: Path, log=print) -> list[str]:
    """Apply outstanding migrations. Returns the filenames applied this run."""
    already = applied(conn)
    done: list[str] = []

    for migration in discover(migrations_dir):
        if migration.version in already:
            if already[migration.version] != migration.checksum():
                log(
                    f"  ! {migration.filename} has changed since it was applied. "
                    f"Migrations are immutable once run — add a new numbered file "
                    f"instead of editing this one."
                )
            continue

        log(f"  → applying {migration.filename}")
        try:
            # executescript() issues a COMMIT before running, which would discard
            # a transaction opened here with execute("BEGIN"). So the BEGIN is
            # prepended to the script itself; executescript does not append a
            # COMMIT, which leaves the transaction open for the bookkeeping insert
            # below. Schema change and its record therefore commit together, and
            # SQLite's DDL is transactional so a failure rolls back cleanly.
            conn.executescript("BEGIN;\n" + migration.sql())
            conn.execute(
                "INSERT INTO schema_migrations (version, filename, checksum, applied_at) "
                "VALUES (?, ?, ?, strftime('%Y-%m-%dT%H:%M:%SZ','now'))",
                (migration.version, migration.filename, migration.checksum()),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            log(f"  ! {migration.filename} failed; rolled back")
            raise

        done.append(migration.filename)

    if not done:
        log("  nothing to apply — schema is current")
    return done
