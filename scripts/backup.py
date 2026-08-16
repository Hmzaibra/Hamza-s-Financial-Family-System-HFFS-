#!/usr/bin/env python3
"""Backup app.db and uploads/.

    python scripts/backup.py [--dest backups] [--keep 14]

The database is copied with SQLite's online backup API, not `cp`. Copying a live
SQLite file with `cp` can capture a torn page mid-write, and in WAL mode it also
misses whatever is still sitting in the -wal file: the copy restores, looks fine,
and is missing the last few transactions.

Restore:
    1. Stop the service:            sudo systemctl stop expenses
    2. Put the database back:       cp backups/<stamp>/app.db ./app.db
       (delete any stale app.db-wal / app.db-shm alongside it)
    3. Put the receipts back:       tar xzf backups/<stamp>/uploads.tar.gz
    4. Start it again:              sudo systemctl start expenses
    5. Confirm:                     sqlite3 app.db "PRAGMA integrity_check;"
"""

from __future__ import annotations

import argparse
import shutil
import sqlite3
import sys
import tarfile
from datetime import datetime, timezone
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent


def backup_database(src: Path, dest: Path) -> None:
    if not src.exists():
        raise SystemExit(f"No database at {src}")

    source = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
    target = sqlite3.connect(str(dest))
    try:
        # Consistent snapshot even while the app is writing.
        source.backup(target)
    finally:
        target.close()
        source.close()

    check = sqlite3.connect(str(dest))
    try:
        result = check.execute("PRAGMA integrity_check").fetchone()[0]
    finally:
        check.close()
    if result != "ok":
        raise SystemExit(f"Backup failed integrity check: {result}")


def backup_uploads(src: Path, dest: Path) -> None:
    if not src.exists():
        return
    with tarfile.open(dest, "w:gz") as tar:
        tar.add(src, arcname="uploads")


def prune(root: Path, keep: int) -> None:
    stamps = sorted((p for p in root.iterdir() if p.is_dir()), reverse=True)
    for old in stamps[keep:]:
        shutil.rmtree(old, ignore_errors=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=BASE_DIR / "app.db", type=Path)
    parser.add_argument("--uploads", default=BASE_DIR / "uploads", type=Path)
    parser.add_argument("--dest", default=BASE_DIR / "backups", type=Path)
    parser.add_argument("--keep", default=14, type=int, help="How many to retain.")
    args = parser.parse_args()

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out = args.dest / stamp
    out.mkdir(parents=True, exist_ok=True)

    backup_database(args.db, out / "app.db")
    backup_uploads(args.uploads, out / "uploads.tar.gz")
    prune(args.dest, args.keep)

    size = sum(f.stat().st_size for f in out.rglob("*") if f.is_file())
    print(f"Backed up to {out} ({size / 1024:.0f} KB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
