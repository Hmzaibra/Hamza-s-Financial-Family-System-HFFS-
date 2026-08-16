"""Phase 0 verification.

Not a test suite — Phase 1 gets one of those. This asserts the things that are
expensive to discover later: that the constraints in 001 actually reject bad
rows, that money never becomes a float, and that the visibility helper fails
closed. Run with: python verify_phase0.py
"""

from __future__ import annotations

import sqlite3
import sys
import tempfile
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import db as dbmod
import money
from migrate import migrate
from visibility import can_edit, can_view, visibility_sql

PASS, FAIL = "  ok  ", "  FAIL"
failures: list[str] = []


def check(label: str, condition: bool) -> None:
    print(f"{PASS if condition else FAIL}  {label}")
    if not condition:
        failures.append(label)


def rejects(conn: sqlite3.Connection, label: str, sql: str, params=()) -> None:
    """Assert the database refuses a write. A silent accept is the bug."""
    try:
        conn.execute(sql, params)
        conn.rollback()
        check(label, False)
    except sqlite3.IntegrityError:
        conn.rollback()
        check(label, True)
    except sqlite3.Error as exc:
        conn.rollback()
        check(f"{label} (raised {type(exc).__name__}: {exc})", True)


def main() -> int:
    tmp = Path(tempfile.mkdtemp()) / "verify.db"
    conn = dbmod.connect(tmp)
    migrate(conn, Path(__file__).resolve().parent / "migrations", log=lambda *_: None)

    print("\nschema")
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    expected = {
        "users", "accounts", "categories", "merchants", "transactions",
        "attachments", "limits", "limit_alerts", "settings", "login_attempts",
    }
    check(f"all {len(expected)} tables present", expected <= tables)
    check("foreign keys enforced on this connection",
          conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1)
    check("journal mode is WAL",
          conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal")
    # 32 from migration 002, plus Reimbursement from 003 — cash is limited to
    # spending, income and reimbursements, and the third needed somewhere to go.
    check("seeded 33 categories",
          conn.execute("SELECT COUNT(*) FROM categories").fetchone()[0] == 33)
    # Migration 004 retired the Receipt-less system merchant: whether there was a
    # receipt is a fact about the transaction, not about who was paid, and the
    # two are independently true.
    check("no system merchant survives",
          conn.execute("SELECT COUNT(*) FROM merchants WHERE is_system = 1").fetchone()[0] == 0)
    check("receipts are a column on the transaction instead",
          "receiptless" in {r[1] for r in conn.execute("PRAGMA table_info(transactions)")})
    check("fx rates have somewhere to be cached",
          "fx_rates" in tables)
    check("base currency is EGP",
          conn.execute("SELECT value FROM settings WHERE key='base_currency'")
              .fetchone()[0] == "EGP")

    # fixtures
    conn.execute(
        "INSERT INTO users (id, username, display_name, password_hash, role, "
        "default_shared, timezone, is_active, created_at) VALUES "
        "(1,'admin','Admin','x','admin',1,'Europe/Berlin',1,'2026-08-15T00:00:00Z'),"
        "(2,'sam','Sam','x','member',1,'Africa/Cairo',1,'2026-08-15T00:00:00Z'),"
        "(3,'other','Other','x','member',0,'Africa/Cairo',1,'2026-08-15T00:00:00Z')"
    )
    conn.execute(
        "INSERT INTO accounts (id, name, type, currency, owner_id, is_active, "
        "sort_order, created_at) VALUES "
        "(1,'CIB Current','bank','EGP',NULL,1,10,'2026-08-15T00:00:00Z'),"
        "(2,'Cash','cash','EGP',2,1,20,'2026-08-15T00:00:00Z'),"
        "(3,'DE Giro','bank','EUR',2,1,30,'2026-08-15T00:00:00Z')"
    )
    conn.commit()

    print("\nconstraints — money and direction")
    base = ("INSERT INTO transactions (user_id, occurred_on, direction, amount_minor, "
            "currency, account_id, is_shared, created_at, updated_at) VALUES ")
    rejects(conn, "negative amount rejected (hard rule 2)",
            base + "(2,'2026-08-15','spend',-500,'EGP',1,1,'t','t')")
    rejects(conn, "zero amount rejected",
            base + "(2,'2026-08-15','spend',0,'EGP',1,1,'t','t')")
    rejects(conn, "unknown direction rejected",
            base + "(2,'2026-08-15','refund',500,'EGP',1,1,'t','t')")
    rejects(conn, "malformed occurred_on rejected",
            base + "(2,'15-08-2026','spend',500,'EGP',1,1,'t','t')")
    rejects(conn, "unknown user_id rejected (FK live)",
            base + "(99,'2026-08-15','spend',500,'EGP',1,1,'t','t')")

    print("\nconstraints — transfers")
    rejects(conn, "transfer without counter account rejected",
            base + "(2,'2026-08-15','transfer',500,'EGP',1,1,'t','t')")
    rejects(
        conn, "transfer without counter amount rejected",
        "INSERT INTO transactions (user_id, occurred_on, direction, amount_minor, currency, "
        "account_id, counter_account_id, is_shared, created_at, updated_at) "
        "VALUES (2,'2026-08-15','transfer',500,'EGP',1,2,1,'t','t')")
    rejects(
        conn, "spend carrying a counter account rejected",
        "INSERT INTO transactions (user_id, occurred_on, direction, amount_minor, currency, "
        "account_id, counter_account_id, is_shared, created_at, updated_at) "
        "VALUES (2,'2026-08-15','spend',500,'EGP',1,2,1,'t','t')")
    rejects(
        conn, "transfer into the same account rejected",
        "INSERT INTO transactions (user_id, occurred_on, direction, amount_minor, currency, "
        "account_id, counter_account_id, counter_amount_minor, counter_currency, "
        "is_shared, created_at, updated_at) "
        "VALUES (2,'2026-08-15','transfer',500,'EGP',1,1,500,'EGP',1,'t','t')")

    # The case that made us add the columns: EUR out, EGP in.
    conn.execute(
        "INSERT INTO transactions (user_id, occurred_on, direction, amount_minor, currency, "
        "account_id, counter_account_id, counter_amount_minor, counter_currency, "
        "is_shared, created_at, updated_at) "
        "VALUES (2,'2026-08-15','transfer',10000,'EUR',3,1,54000000,'EGP',1,'t','t')")
    conn.commit()
    row = conn.execute(
        "SELECT amount_minor, currency, counter_amount_minor, counter_currency "
        "FROM transactions WHERE direction='transfer'").fetchone()
    check("cross-currency transfer records both sides",
          (row[0], row[1], row[2], row[3]) == (10000, "EUR", 54000000, "EGP"))

    print("\nconstraints — categories and uniqueness")
    rejects(conn, "three-level category nesting rejected",
            "INSERT INTO categories (name, parent_id) VALUES ('Too deep', 101)")
    rejects(conn, "duplicate top-level category rejected",
            "INSERT INTO categories (name, parent_id) VALUES ('Groceries', NULL)")
    # NOCASE on merchants.name is what stops the entry form's inline add creating
    # both "Carrefour" and "carrefour". It used to be tested against the seeded
    # Receipt-less row, which migration 004 retired, so it seeds its own.
    conn.execute("INSERT INTO merchants (name, is_system, is_active, created_at) "
                 "VALUES ('Carrefour', 0, 1, 't')")
    rejects(conn, "case-variant merchant rejected",
            "INSERT INTO merchants (name, is_system, is_active, created_at) "
            "VALUES ('carrefour', 0, 1, 't')")
    rejects(conn, "household limit with a scope_id rejected",
            "INSERT INTO limits (name, scope_type, scope_id, period, amount_minor, "
            "currency, created_at) VALUES ('x','household',5,'monthly',1000,'EGP','t')")
    rejects(conn, "duplicate limit alert rejected",
            "INSERT INTO limit_alerts (limit_id, period_key, threshold_pct, spent_minor, "
            "sent_at) VALUES (1,'2026-08',80,1,'t')")

    print("\nattachments cascade")
    txn_id = conn.execute("SELECT id FROM transactions").fetchone()[0]
    conn.execute(
        "INSERT INTO attachments (transaction_id, file_path, thumb_path, mime, byte_size, "
        "created_at) VALUES (?, '2026/08/a.jpg', '2026/08/a_t.jpg', 'image/jpeg', 1, 't')",
        (txn_id,))
    conn.execute("DELETE FROM transactions WHERE id = ?", (txn_id,))
    conn.commit()
    check("deleting a transaction cascades its attachments",
          conn.execute("SELECT COUNT(*) FROM attachments").fetchone()[0] == 0)

    print("\nmoney")
    check("'12.34' EGP  → 1234 minor", money.parse_to_minor("12.34", "EGP") == 1234)
    check("'1,250' EGP  → 125000 minor", money.parse_to_minor("1,250", "EGP") == 125000)
    check("'0.1' EGP    → 10 minor", money.parse_to_minor("0.1", "EGP") == 10)
    check("'100' JPY    → 100 minor (exponent 0)", money.parse_to_minor("100", "JPY") == 100)
    check("'1.5' KWD    → 1500 minor (exponent 3)", money.parse_to_minor("1.5", "KWD") == 1500)
    check("'0.005' EGP  → 1 minor (half-up, not banker's)",
          money.parse_to_minor("0.005", "EGP") == 1)
    check("float input refused", _raises(money.parse_to_minor, 12.34, "EGP"))
    check("negative refused", _raises(money.parse_to_minor, "-5", "EGP"))
    check("zero refused", _raises(money.parse_to_minor, "0", "EGP"))
    check("gibberish refused", _raises(money.parse_to_minor, "abc", "EGP"))
    check("format 125000 EGP → '1,250.00'", money.format_minor(125000, "EGP") == "1,250.00")
    check("format 100 JPY    → '100'", money.format_minor(100, "JPY") == "100")
    check("format 1500 KWD   → '1.500'", money.format_minor(1500, "KWD") == "1.500")
    check("fx conversion returns int",
          isinstance(money.convert_to_base(10000, 54.0), int))
    check("10000 EUR minor @ 54.0 → 540000 EGP minor",
          money.convert_to_base(10000, 54.0) == 540000)
    check("no float anywhere in the parse path",
          isinstance(money.parse_to_minor(Decimal("9.99"), "EGP"), int))

    print("\nvisibility (section 4)")
    admin = {"id": 1, "role": "admin"}
    me = {"id": 2, "role": "member"}
    sql_admin, p_admin = visibility_sql(admin)
    sql_me, p_me = visibility_sql(me)
    check("admin fragment is unconditional", (sql_admin, p_admin) == ("1 = 1", []))
    check("member fragment is owner-or-shared",
          sql_me == "(t.user_id = ? OR t.is_shared = 1)" and p_me == [2])
    check("no session fails closed", visibility_sql(None) == ("0 = 1", []))
    check("alias is honoured", visibility_sql(me, alias="tx")[0].startswith("(tx.user_id"))

    mine_private = {"id": 10, "user_id": 2, "is_shared": 0}
    theirs_private = {"id": 11, "user_id": 3, "is_shared": 0}
    theirs_shared = {"id": 12, "user_id": 3, "is_shared": 1}
    check("member sees own private", can_view(me, mine_private))
    check("member does not see another's private", not can_view(me, theirs_private))
    check("member sees another's shared", can_view(me, theirs_shared))
    check("admin sees another's private", can_view(admin, theirs_private))
    check("seeing a shared row does not confer edit", not can_edit(me, theirs_shared))
    check("owner may edit own row", can_edit(me, mine_private))
    check("admin may edit any row", can_edit(admin, theirs_private))

    # The fragment has to survive contact with a real query.
    conn.execute(
        "INSERT INTO transactions (user_id, occurred_on, direction, amount_minor, currency, "
        "account_id, is_shared, created_at, updated_at) VALUES "
        "(2,'2026-08-14','spend',1000,'EGP',1,0,'t','t'),"
        "(3,'2026-08-14','spend',2000,'EGP',1,0,'t','t'),"
        "(3,'2026-08-14','spend',3000,'EGP',1,1,'t','t')")
    conn.commit()
    seen = conn.execute(
        f"SELECT SUM(t.amount_minor) FROM transactions t WHERE {sql_me}", p_me).fetchone()[0]
    check("member's filtered total excludes another's private row (1000+3000)", seen == 4000)
    seen_admin = conn.execute(
        f"SELECT SUM(t.amount_minor) FROM transactions t WHERE {sql_admin}",
        p_admin).fetchone()[0]
    check("admin's total includes everything (1000+2000+3000)", seen_admin == 6000)

    print("\ntimezone")
    check("per-user zones resolve",
          dbmod.today_for("Africa/Cairo") is not None
          and dbmod.today_for("Europe/Berlin") is not None)
    check("bad zone falls back instead of raising", dbmod.today_for("Not/AZone") is not None)
    check("utc_now is ISO-8601 Z", dbmod.utc_now().endswith("Z") and "T" in dbmod.utc_now())

    conn.close()

    print()
    if failures:
        print(f"{len(failures)} check(s) failed:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("all checks passed")
    return 0


def _raises(fn, *args) -> bool:
    try:
        fn(*args)
        return False
    except money.MoneyError:
        return True
    except Exception:
        return False


if __name__ == "__main__":
    sys.exit(main())
