"""Verification for budgets and the Telegram sweep.

The expensive failures in here are all quiet ones.

A budget that reads low because it silently filtered by the reader looks exactly
like a budget that is being met. A sweep that records an alert it never managed
to send looks exactly like a sweep that warned you. And a `check-limits` that
found its way into a request would look like nothing at all until the day
api.telegram.org is slow and the entry form hangs in a supermarket basement.

So the sends are stubbed rather than mocked away: `telegram.send` is replaced
with something that records its arguments, and the checks are about who was
told, how often, and what was written down afterwards.
"""

from __future__ import annotations

import re
import sys
import tempfile
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import db as dbmod
import limits as budgets
import telegram
from app import create_app
from config import Config
from migrate import migrate
from werkzeug.security import generate_password_hash

failures: list[str] = []


def check(label: str, condition: bool) -> None:
    print(f"{'  ok  ' if condition else '  FAIL'}  {label}")
    if not condition:
        failures.append(label)


def token(html: bytes) -> str:
    m = re.search(rb'name="_csrf" value="([^"]+)"', html)
    return m.group(1).decode() if m else ""


class Outbox:
    """Stands in for telegram.send and remembers what it was asked to do."""

    def __init__(self, fail_on=()):
        self.sent: list[tuple[str, str]] = []
        self.fail_on = set(fail_on)

    def __call__(self, token_value, chat_id, text):
        if chat_id in self.fail_on:
            raise telegram.TelegramError("bot was blocked by the user")
        self.sent.append((chat_id, text))

    @property
    def chats(self) -> set:
        return {chat for chat, _ in self.sent}


def main() -> int:
    tmpdir = Path(tempfile.mkdtemp())

    class T(Config):
        DATABASE_PATH = tmpdir / "limits.db"
        UPLOAD_DIR = tmpdir / "uploads"
        SECRET_KEY = "test-key"
        SESSION_COOKIE_SECURE = False
        TELEGRAM_BOT_TOKEN = "test-token"

    conn = dbmod.connect(T.DATABASE_PATH)
    migrate(conn, Config.MIGRATIONS_DIR, log=lambda *_: None)
    pw = generate_password_hash("pw12345678")
    conn.execute(
        "INSERT INTO users (id, username, display_name, password_hash, role, default_shared, "
        "timezone, telegram_chat_id, is_active, created_at) VALUES "
        "(1,'admin','Admin',?,'admin',1,'Africa/Cairo','1001',1,'t'),"
        "(2,'mem','Member',?,'member',1,'Africa/Cairo','1002',1,'t'),"
        "(3,'quiet','Quiet',?,'member',1,'Africa/Cairo',NULL,1,'t')", (pw, pw, pw))
    # CIB with a debit card hanging off it, so the settlement rule can be tested.
    conn.execute(
        "INSERT INTO accounts (id, name, type, currency, parent_account_id, "
        "opening_balance_minor, is_active, sort_order, created_at) VALUES "
        "(1,'CIB','bank','EGP',NULL,1000000,1,10,'t'),"
        "(2,'DE Giro','bank','EUR',NULL,100000,1,30,'t')")
    conn.execute(
        "INSERT INTO accounts (id, name, type, currency, parent_account_id, "
        "opening_balance_minor, is_active, sort_order, created_at, card_network, "
        "card_expires_on, withdrawal_limit_minor, card_color) VALUES "
        "(3,'CIB Debit','debit_card','EGP',1,0,1,40,'t','Visa','2099-12',600000,'#1F6F63')")
    # High ids: 002 seeds the real category tree, and colliding with it would
    # make these checks depend on what that seed happens to contain.
    conn.execute(
        "INSERT INTO categories (id, name, parent_id, is_active, sort_order) VALUES "
        "(901,'Fixture Food',NULL,1,10), (902,'Fixture Coffee',901,1,20), "
        "(903,'Fixture Transport',NULL,1,30)")
    conn.execute(
        "INSERT INTO merchants (id, name, kind, default_is_online, is_system, is_active, "
        "created_at) VALUES (60,'Seoudi','spend',0,0,1,'t'), (61,'Uber','spend',0,0,1,'t')")
    conn.commit()
    conn.close()

    app = create_app(T)
    app.config.update(TESTING=True)

    def sql(q, p=()):
        c = dbmod.connect(T.DATABASE_PATH)
        try:
            return c.execute(q, p).fetchall()
        finally:
            c.close()

    def run(q, p=()):
        c = dbmod.connect(T.DATABASE_PATH)
        try:
            cur = c.execute(q, p)
            c.commit()
            return cur.lastrowid
        finally:
            c.close()

    def txn(**kw) -> int:
        cols = {
            "user_id": 1, "occurred_on": "2026-08-10", "direction": "spend",
            "amount_minor": 10000, "currency": "EGP", "fx_rate_to_base": None,
            "account_id": 1, "counter_account_id": None, "counter_amount_minor": None,
            "counter_currency": None, "merchant_id": None, "category_id": None,
            "is_online": 0, "note": None, "is_shared": 1, "receiptless": 0,
            "created_at": "t", "updated_at": "t",
        }
        cols.update(kw)
        return run(f"INSERT INTO transactions ({','.join(cols)}) "
                   f"VALUES ({','.join('?' for _ in cols)})", tuple(cols.values()))

    def limit(**kw) -> int:
        cols = {"name": "Budget", "scope_type": "household", "scope_id": None,
                "period": "monthly", "amount_minor": 100000, "currency": "EGP",
                "warn_pct": 80, "is_active": 1, "created_at": "t"}
        cols.update(kw)
        return run(f"INSERT INTO limits ({','.join(cols)}) "
                   f"VALUES ({','.join('?' for _ in cols)})", tuple(cols.values()))

    def row(limit_id):
        return sql("SELECT * FROM limits WHERE id = ?", (limit_id,))[0]

    def login(client, username="admin"):
        page = client.get("/login").data
        client.post("/login", data={"username": username, "password": "pw12345678",
                                    "_csrf": token(page)})

    AUG = date(2026, 8, 15)          # a Saturday, ISO week 33
    admin = {"id": 1, "role": "admin", "timezone": "Africa/Cairo", "default_shared": 1}
    member = {"id": 2, "role": "member", "timezone": "Africa/Cairo", "default_shared": 1}

    # ---------------------------------------------------------------- periods

    print("\nperiods have edges anyone can find")
    check("a month is its own name", budgets.period_key("monthly", AUG) == "2026-08")
    check("a week is the ISO one, which is what a phone calendar shows",
          budgets.period_key("weekly", AUG) == "2026-W33")
    check("the week starts on Monday",
          budgets.period_bounds("weekly", AUG)[0] == "2026-08-10")
    check("and is half-open, so no purchase lands in two weeks",
          budgets.period_bounds("weekly", AUG)[1] == "2026-08-17")
    check("a month ends at the 1st of the next",
          budgets.period_bounds("monthly", AUG) == ("2026-08-01", "2026-09-01"))
    check("December rolls the year rather than the month",
          budgets.period_bounds("monthly", date(2026, 12, 9))[1] == "2027-01-01")
    check("a week spanning New Year keeps its ISO year",
          budgets.period_key("weekly", date(2026, 12, 31)) == "2026-W53")
    check("the label is readable without a strftime that only works on Linux",
          "week of 10 August" == budgets.period_label("weekly", AUG))

    # ----------------------------------------------------------------- scopes

    print("\nwhat a budget is about")
    txn(amount_minor=10000, user_id=1, category_id=902, account_id=1, merchant_id=60)
    txn(amount_minor=20000, user_id=2, category_id=903, account_id=3, merchant_id=61)
    txn(amount_minor=30000, user_id=1, category_id=901, account_id=1, merchant_id=60)
    # Outside August, so every window below has to exclude it.
    txn(amount_minor=99999, occurred_on="2026-07-04")

    with app.app_context(), app.test_request_context():
        household = row(limit(name="Everything"))
        check("household counts everyone, this period only",
              budgets.spent(household, AUG)[0] == 60000)

        one_person = row(limit(name="Member", scope_type="user", scope_id=2))
        check("a person's budget counts only theirs",
              budgets.spent(one_person, AUG)[0] == 20000)

        parent = row(limit(name="Food", scope_type="category", scope_id=901))
        check("a parent category counts its children — 300 in Food plus 100 in Coffee",
              budgets.spent(parent, AUG)[0] == 40000)

        child = row(limit(name="Coffee", scope_type="category", scope_id=902))
        check("a child category counts only itself", budgets.spent(child, AUG)[0] == 10000)

        bank = row(limit(name="CIB", scope_type="account", scope_id=1))
        check("an account counts the cards that draw on it — the settlement rule",
              budgets.spent(bank, AUG)[0] == 60000)

        card = row(limit(name="Card", scope_type="account", scope_id=3))
        check("and a budget on the card itself resolves to the same account",
              budgets.spent(card, AUG)[0] == 60000)

        shop = row(limit(name="Seoudi", scope_type="merchant", scope_id=60))
        check("a merchant counts what was spent there", budgets.spent(shop, AUG)[0] == 40000)

        weekly = row(limit(name="This week", period="weekly"))
        check("a weekly budget sees only its week", budgets.spent(weekly, AUG)[0] == 60000)
        check("and nothing from the week before",
              budgets.spent(weekly, date(2026, 8, 5))[0] == 0)

    print("\nforeign spending is converted, or counted and admitted to")
    with app.app_context(), app.test_request_context():
        txn(amount_minor=1000, currency="EUR", fx_rate_to_base=55.0)   # 550.00 EGP
        txn(amount_minor=5000, currency="USD", fx_rate_to_base=None)   # no rate captured
        household = row(1)
        total, missing = budgets.spent(household, AUG)
        check("a rate captured at entry converts into the base currency", total == 60000 + 55000)
        check("one without a rate is not guessed at", missing == 1)
        state = budgets.evaluate(household, AUG)
        check("and the screen is told, rather than quietly reading low",
              state["unconverted"] == 1)
        check("the message says so too",
              "not counted" in budgets.message(state, 100, "EGP"))

    # ------------------------------------------------------------ evaluation

    print("\nwhat a budget bar says")
    with app.app_context(), app.test_request_context():
        small = row(limit(name="Tight", amount_minor=100000, warn_pct=80))
        state = budgets.evaluate(small, AUG)
        check("spent is a whole-piastre integer, never a float",
              isinstance(state["spent_minor"], int))
        check("the percentage is integer maths", state["pct"] == 115)
        check("over its end", state["over"] and state["warning"])
        check("the printed number keeps going past 100", state["pct"] > 100)
        check("the bar does not — an overflowing track is a rendering bug",
              state["bar_pct"] == 100)
        check("remaining goes negative, which is the honest answer",
              state["remaining_minor"] == 100000 - state["spent_minor"])

        roomy = row(limit(name="Roomy", amount_minor=100_000_00, warn_pct=80))
        state = budgets.evaluate(roomy, AUG)
        check("a budget nowhere near its mark is not a warning", not state["warning"])
        check("and not over", not state["over"])

    # -------------------------------------------------------- the write rules

    print("\nthe database refuses a budget that could not be measured")
    try:
        limit(name="In dollars", currency="USD")
        check("a budget in a currency the household does not count in is refused", False)
    except Exception as exc:
        check("a budget in a currency the household does not count in is refused",
              "base currency" in str(exc))

    try:
        limit(name="Nameless", scope_type="category", scope_id=None)
        check("a scoped budget that names nothing is refused", False)
    except Exception as exc:
        check("a scoped budget that names nothing is refused",
              "what it is about" in str(exc) or "CHECK" in str(exc))

    try:
        limit(name="Odd", scope_type="household", scope_id=1)
        check("and a household budget that names something is too", False)
    except Exception:
        check("and a household budget that names something is too", True)

    # ---------------------------------------------------------- who sees what

    print("\nwhich budgets a person may see at all")
    with app.app_context(), app.test_request_context():
        mine = limit(name="Member's own", scope_type="user", scope_id=2)
        theirs = limit(name="Admin's own", scope_type="user", scope_id=1)

        visible_to_member = {r["id"] for r in budgets.visible_limits(member)}
        check("a member sees their own personal budget", mine in visible_to_member)
        check("and never another person's", theirs not in visible_to_member)
        check("household, category, account and merchant budgets are shared facts",
              1 in visible_to_member)
        check("admin sees everything",
              theirs in {r["id"] for r in budgets.visible_limits(admin)})
        check("nobody signed in sees nothing at all", budgets.visible_limits(None) == [])

        # The stated exception: the figures inside a visible budget are read
        # across the whole household. A member's view of the household budget
        # has to be the same number the admin sees, or it is not a budget.
        as_admin = {b["name"]: b["spent_minor"] for b in budgets.dashboard_limits(admin, AUG)}
        as_member = {b["name"]: b["spent_minor"] for b in budgets.dashboard_limits(member, AUG)}
        check("and the figures inside are the same for both — a filtered budget "
              "is a wrong number, not a partial one",
              as_member["Everything"] == as_admin["Everything"])

    # ----------------------------------------------------------- the thresholds

    print("\nwarnings are said once and then not again")
    with app.app_context(), app.test_request_context():
        watched = row(limit(name="Watched", amount_minor=50000, warn_pct=80))
        state = budgets.evaluate(watched, AUG)
        due = budgets.due_thresholds(watched, state)
        check("a budget already past both marks owes both messages", due == [80, 100])

        budgets.record(watched["id"], state["period_key"], 80, state["spent_minor"])
        check("once the warning is recorded only the full one is left",
              budgets.due_thresholds(watched, state) == [100])

        budgets.record(watched["id"], state["period_key"], 100, state["spent_minor"])
        check("and then nothing", budgets.due_thresholds(watched, state) == [])

        september = budgets.evaluate(watched, date(2026, 9, 15))
        check("a new period starts the conversation over",
              september["period_key"] == "2026-09")

        quiet = row(limit(name="Quiet", amount_minor=100_000_00, warn_pct=80))
        check("a budget under its mark owes nothing",
              budgets.due_thresholds(quiet, budgets.evaluate(quiet, AUG)) == [])

    # ---------------------------------------------------------------- the sweep

    print("\nwho gets told")
    with app.app_context(), app.test_request_context():
        run("DELETE FROM limits")
        run("DELETE FROM limit_alerts")

        outbox = Outbox()
        telegram.send = outbox
        about_member = limit(name="Member's food", scope_type="user", scope_id=2,
                             amount_minor=10000, warn_pct=80)
        budgets.sweep(AUG, "test-token")
        check("a budget about one person messages that person",
              "1002" in outbox.chats)
        check("and admin as well, who set it and will be asked about it",
              "1001" in outbox.chats)

        run("DELETE FROM limits")
        run("DELETE FROM limit_alerts")
        outbox = Outbox()
        telegram.send = outbox
        limit(name="Coffee", scope_type="category", scope_id=902, amount_minor=1000)
        budgets.sweep(AUG, "test-token")
        check("a category budget has no single subject, so it goes to admin only",
              outbox.chats == {"1001"})

    print("\nnothing is recorded that was not actually sent")
    with app.app_context(), app.test_request_context():
        run("DELETE FROM limits")
        run("DELETE FROM limit_alerts")

        # Admin's phone has blocked the bot. Nobody hears anything.
        telegram.send = Outbox(fail_on={"1001"})
        broken = limit(name="Broken pipe", scope_type="category", scope_id=902,
                       amount_minor=1000)
        lines = budgets.sweep(AUG, "test-token")
        check("a failed send is reported", any("blocked" in line for line in lines))
        check("and nothing is written down, so the next run tries again",
              sql("SELECT COUNT(*) FROM limit_alerts")[0][0] == 0)

        # Same budget, working phone. The warning it was owed arrives.
        outbox = Outbox()
        telegram.send = outbox
        budgets.sweep(AUG, "test-token")
        check("the retry delivers what was owed", len(outbox.sent) == 2)
        check("and records it", sql("SELECT COUNT(*) FROM limit_alerts")[0][0] == 2)

        outbox = Outbox()
        telegram.send = outbox
        budgets.sweep(AUG, "test-token")
        check("running again the same hour sends nothing — cron may be generous",
              outbox.sent == [])

    print("\nthe cases where there is nobody to tell")
    with app.app_context(), app.test_request_context():
        run("DELETE FROM limits")
        run("DELETE FROM limit_alerts")
        run("UPDATE users SET telegram_chat_id = NULL")

        outbox = Outbox()
        telegram.send = outbox
        limit(name="Unheard", scope_type="category", scope_id=902, amount_minor=1000)
        lines = budgets.sweep(AUG, "test-token")
        check("with no chat ids anywhere, nothing is sent", outbox.sent == [])
        check("the log says how to fix it",
              any("telegram-chats" in line for line in lines))
        check("and nothing is recorded, so it arrives the day someone pastes an id in",
              sql("SELECT COUNT(*) FROM limit_alerts")[0][0] == 0)

        run("UPDATE users SET telegram_chat_id = '1001' WHERE id = 1")
        outbox = Outbox()
        telegram.send = outbox
        lines = budgets.sweep(AUG, "test-token", dry_run=True)
        check("a dry run decides everything and sends nothing", outbox.sent == [])
        check("but says what it would have done", any("would send" in line for line in lines))
        check("and records nothing", sql("SELECT COUNT(*) FROM limit_alerts")[0][0] == 0)

    print("\na budget left behind by a change of base currency")
    with app.app_context(), app.test_request_context():
        run("DELETE FROM limits")
        run("DELETE FROM limit_alerts")
        stale_id = limit(name="Old money", amount_minor=100)
        run("UPDATE settings SET value = 'EUR' WHERE key = 'base_currency'")
        run("INSERT OR IGNORE INTO settings (key, value, updated_at) "
            "VALUES ('base_currency','EUR','t')")

        outbox = Outbox()
        telegram.send = outbox
        lines = budgets.sweep(AUG, "test-token")
        check("a budget set in the old base currency is not compared against the new one",
              outbox.sent == [])
        check("it says so instead of guessing", any("skipped" in line for line in lines))
        run("UPDATE settings SET value = 'EGP' WHERE key = 'base_currency'")
        run("DELETE FROM limits WHERE id = ?", (stale_id,))

    # ----------------------------------------------------------------- screens

    print("\nthe month screen")
    with app.app_context(), app.test_request_context():
        run("DELETE FROM limits")
        run("DELETE FROM limit_alerts")
        limit(name="Household August", amount_minor=50000)
        limit(name="Member only", scope_type="user", scope_id=2, amount_minor=50000)

    with app.test_client() as c:
        login(c, "mem")
        page = c.get("/dashboard").data
        check("a budget appears on the month screen", b"Household August" in page)
        check("with a bar", b"sharebar__fill" in page)
        check("coloured for being over rather than shouting about it",
              b"sharebar__fill--over" in page)
        check("a member sees a budget that is about them", b"Member only" in page)

    with app.app_context(), app.test_request_context():
        limit(name="Admin only", scope_type="user", scope_id=1, amount_minor=50000)

    with app.test_client() as c:
        login(c, "mem")
        check("and never one about somebody else",
              b"Admin only" not in c.get("/dashboard").data)

    print("\nsetting one up")
    with app.test_client() as c:
        login(c)
        page = c.get("/settings/limits/new").data
        check("the form says budgets warn and never block", b"never stops" in page
              or b"warns" in page)
        check("what it is about is one control, so it cannot be filled in "
              "inconsistently with JavaScript off", page.count(b'name="scope"') == 1)
        check("with the four kinds grouped inside it", page.count(b"<optgroup") == 4)

        r = c.post("/settings/limits/new", data={
            "_csrf": token(page), "name": "Transport", "scope": "category:903",
            "amount": "1500", "period": "monthly", "warn_pct": "75", "is_active": "1",
        }, follow_redirects=True)
        check("it saves", b"Saved Transport" in r.data)
        made = sql("SELECT * FROM limits WHERE name = 'Transport'")[0]
        check("the kind and the id are split back apart",
              made["scope_type"] == "category" and made["scope_id"] == 903)
        check("and the amount is stored in minor units", made["amount_minor"] == 150000)

        page = c.get("/settings/limits/new").data
        r = c.post("/settings/limits/new", data={
            "_csrf": token(page), "name": "Bad", "scope": "category:903",
            "amount": "-5", "period": "monthly", "warn_pct": "80",
        })
        check("a negative budget is a sentence, not a constraint name",
              r.status_code == 400 and b"cannot be negative" in r.data)

        r = c.post("/settings/limits/new", data={
            "_csrf": token(page), "name": "Bad", "scope": "category:903",
            "amount": "100", "period": "monthly", "warn_pct": "400",
        })
        check("so is a warning mark outside 1–100", b"between 1 and 100" in r.data)

        # Lowering a budget mid-month past a mark it already spoke about.
        with app.app_context(), app.test_request_context():
            budgets.record(made["id"], "2026-08", 80, 1)
        page = c.get(f"/settings/limits/{made['id']}").data
        c.post(f"/settings/limits/{made['id']}", data={
            "_csrf": token(page), "name": "Transport", "scope": "category:903",
            "amount": "10", "period": "monthly", "warn_pct": "75", "is_active": "1",
        })
        check("editing a budget lets the new number speak, rather than being "
              "silenced by the old one's alert",
              sql("SELECT COUNT(*) FROM limit_alerts WHERE limit_id = ? AND period_key = ?",
                  (made["id"], "2026-08"))[0][0] == 0)

    print("\npeople, which is where a chat id gets set")
    with app.test_client() as c:
        login(c)
        page = c.get("/settings/people").data
        check("the screen lists everyone", b"Member" in page and b"Quiet" in page)
        check("and points out who cannot be reached", b"no Telegram" in page)

        page = c.get("/settings/people/new").data
        r = c.post("/settings/people/new", data={
            "_csrf": token(page), "username": "sam", "display_name": "Sam",
            "role": "member", "timezone": "Africa/Cairo", "password": "pw12345678",
            "is_active": "1", "default_shared": "1",
        }, follow_redirects=True)
        check("a family member can be added without touching sqlite3", b"Added Sam" in r.data)

        r = c.post("/settings/people/new", data={
            "_csrf": token(page), "username": "sam2", "display_name": "Sam",
            "role": "member", "timezone": "Africa/Cairo", "password": "short",
            "is_active": "1",
        })
        check("a short password is refused", b"at least 8" in r.data)

        r = c.post("/settings/people/new", data={
            "_csrf": token(page), "username": "has space", "display_name": "X",
            "role": "member", "timezone": "Africa/Cairo", "password": "pw12345678",
        })
        check("a username with a space in it is refused", b"dots, dashes" in r.data)

        r = c.post("/settings/people/2", data={
            "_csrf": token(page), "display_name": "Member", "role": "member",
            "timezone": "Africa/Cairo", "telegram_chat_id": "not-a-number",
            "is_active": "1",
        })
        check("a chat id that is not a number is refused", b"is a number" in r.data)

        r = c.post("/settings/people/2", data={
            "_csrf": token(page), "display_name": "Member", "role": "member",
            "timezone": "Africa/Cairo", "telegram_chat_id": "-100200", "is_active": "1",
        }, follow_redirects=True)
        check("a group chat's negative id is not a typo", b"Saved Member" in r.data)

        r = c.post("/settings/people/1", data={
            "_csrf": token(page), "display_name": "Admin", "role": "member",
            "timezone": "Africa/Cairo", "is_active": "1",
        })
        check("the last admin cannot demote themselves out of Setup",
              b"only admin left" in r.data)

        r = c.post("/settings/people/1", data={
            "_csrf": token(page), "display_name": "Admin", "role": "admin",
            "timezone": "Africa/Cairo",
        })
        check("nor switch themselves off", b"only admin left" in r.data)

        page2 = c.get("/settings/people/1").data
        check("and the username is shown rather than offered for editing",
              b'name="username"' not in page2)

    # ------------------------------------------------------- the source rules

    print("\nthe rules that only reading the source can check")
    blueprint_source = "\n".join(
        p.read_text(encoding="utf-8") for p in Path("blueprints").glob("*.py"))
    # The import statement specifically. A blueprint may perfectly well mention
    # telegram_chat_id or read TELEGRAM_BOT_TOKEN out of config — what it may
    # not do is get hold of the module that opens the socket.
    check("no blueprint imports telegram — invariant 7",
          not re.search(r"^\s*(import telegram|from telegram import)",
                        blueprint_source, re.M))
    check("and none of them runs the sweep from a request",
          "sweep(" not in blueprint_source)
    check("the sweep lives behind a CLI command instead",
          "check-limits" in Path("app.py").read_text(encoding="utf-8"))
    limits_source = Path("limits.py").read_text(encoding="utf-8")
    check("limits.py imports telegram inside the sweep, not at module scope — "
          "the dashboard imports this file on every page view",
          "\nimport telegram" not in limits_source)
    check("and says out loud that it is the second exception to rule 4",
          "visibility_sql" in limits_source and "exception" in limits_source)

    print()
    if failures:
        print(f"{len(failures)} check(s) failed:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
