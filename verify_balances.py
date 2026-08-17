"""Verification for balances, the month figures, and editing an entry.

The arithmetic here is the part nobody notices being wrong. A balance that is
off by one transfer leg looks exactly like a balance that is right, and the only
thing standing between the two is a check that says what the number should be.
"""

from __future__ import annotations

import re
import sys
import tempfile
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import balances as bal
import db as dbmod
from app import create_app
from config import Config
from migrate import migrate
from visibility import visibility_sql
from werkzeug.security import generate_password_hash

failures: list[str] = []


def check(label: str, condition: bool) -> None:
    print(f"{'  ok  ' if condition else '  FAIL'}  {label}")
    if not condition:
        failures.append(label)


def token(html: bytes) -> str:
    m = re.search(rb'name="_csrf" value="([^"]+)"', html)
    return m.group(1).decode() if m else ""


def main() -> int:
    tmpdir = Path(tempfile.mkdtemp())

    class T(Config):
        DATABASE_PATH = tmpdir / "bal.db"
        UPLOAD_DIR = tmpdir / "uploads"
        SECRET_KEY = "test-key"
        SESSION_COOKIE_SECURE = False

    conn = dbmod.connect(T.DATABASE_PATH)
    migrate(conn, Config.MIGRATIONS_DIR, log=lambda *_: None)
    pw = generate_password_hash("pw12345678")
    conn.execute(
        "INSERT INTO users (id, username, display_name, password_hash, role, default_shared, "
        "timezone, is_active, created_at) VALUES "
        "(1,'admin','Admin',?,'admin',1,'Africa/Cairo',1,'2026-08-16T00:00:00Z'),"
        "(2,'mem','Member',?,'member',0,'Africa/Cairo',1,'2026-08-16T00:00:00Z')", (pw, pw))
    # CIB opens with 1,000.00 EGP; a debit card and an Instapay handle hang off
    # it; cash opens with 50.00; a EUR bank stands alone.
    conn.execute(
        "INSERT INTO accounts (id, name, type, currency, parent_account_id, "
        "opening_balance_minor, is_active, sort_order, created_at) VALUES "
        "(1,'CIB','bank','EGP',NULL,100000,1,10,'t'),"
        "(2,'Cash','cash','EGP',NULL,5000,1,20,'t'),"
        "(3,'DE Giro','bank','EUR',NULL,20000,1,30,'t')")
    # Cards carry their own required fields; expiry is far enough out that these
    # checks do not start failing on a calendar boundary.
    conn.execute(
        "INSERT INTO accounts (id, name, type, currency, parent_account_id, "
        "opening_balance_minor, is_active, sort_order, created_at, card_network, "
        "card_expires_on, withdrawal_limit_minor, instapay_handle, card_color, credit_limit_local_minor, credit_limit_intl_minor) VALUES "
        "(4,'CIB Debit','debit_card','EGP',1,0,1,40,'t','Visa','2099-12',600000,NULL,'#1F6F63',NULL,NULL),"
        "(5,'Sam - @sam_pay','instapay','EGP',1,0,1,50,'t',NULL,NULL,NULL,'@sam_pay',NULL,NULL,NULL),"
        "(6,'AmEx','credit_card','EGP',NULL,0,1,60,'t','Visa','2099-12',600000,NULL,'#C0503C',5000000,5000000)")
    conn.execute(
        "INSERT INTO merchants (id, name, kind, default_is_online, is_system, is_active, created_at) "
        "VALUES (60,'Seoudi','spend',0,0,1,'t'), (61,'Payroll','income',0,0,1,'t')")
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

    def txn(**kw):
        cols = {
            "user_id": 1, "occurred_on": "2026-08-10", "direction": "spend",
            "amount_minor": 0, "currency": "EGP", "fx_rate_to_base": None,
            "account_id": 1, "counter_account_id": None, "counter_amount_minor": None,
            "counter_currency": None, "merchant_id": None, "category_id": None,
            "is_online": 0, "note": None, "is_shared": 1, "receiptless": 0,
            "created_at": "2026-08-10T00:00:00Z", "updated_at": "2026-08-10T00:00:00Z",
        }
        cols.update(kw)
        c = dbmod.connect(T.DATABASE_PATH)
        try:
            c.execute(
                f"INSERT INTO transactions ({','.join(cols)}) "
                f"VALUES ({','.join('?' for _ in cols)})", tuple(cols.values()))
            c.commit()
        finally:
            c.close()

    def balances():
        with app.app_context():
            with app.test_request_context():
                return bal.balances_for_display()

    print("\nan empty ledger is just the opening balances")
    with app.app_context(), app.test_request_context():
        b = bal.balances_for_display()
        check("a bank opens where it was told to", b[1].minor == 100000)
        check("cash too", b[2].minor == 5000)
        check("a credit card opens at zero", b[6].minor == 0)
        check("and each is in its own currency", b[3].currency == "EUR" and b[1].currency == "EGP")

    print("\nthe three ways a transaction moves a balance")
    txn(direction="spend", amount_minor=25000, account_id=1)
    with app.app_context(), app.test_request_context():
        check("spending reduces the account it left", bal.balances_for_display()[1].minor == 75000)
    txn(direction="income", amount_minor=40000, account_id=1, merchant_id=61)
    with app.app_context(), app.test_request_context():
        check("income increases it", bal.balances_for_display()[1].minor == 115000)

    txn(direction="transfer", amount_minor=10000, account_id=1,
        counter_account_id=3, counter_amount_minor=200, counter_currency="EUR")
    with app.app_context(), app.test_request_context():
        b = bal.balances_for_display()
        check("a transfer leaves the source", b[1].minor == 105000)
        check("and arrives at the destination in its own currency", b[3].minor == 20200)

    print("\nmoney spent on a card comes out of the account behind it")
    txn(direction="spend", amount_minor=5000, account_id=4)   # the debit card
    with app.app_context(), app.test_request_context():
        b = bal.balances_for_display()
        check("a debit card charge reduces its parent", b[1].minor == 100000)
        check("the card reports the parent's balance", b[4].minor == b[1].minor)
        check("so does the Instapay handle", b[5].minor == b[1].minor)
        check("and the card is not a second pot", b[4] is b[1])

    txn(direction="spend", amount_minor=1000, account_id=5)   # via Instapay
    with app.app_context(), app.test_request_context():
        check("spending over Instapay also lands on the parent",
              bal.balances_for_display()[1].minor == 99000)

    print("\na credit card is the only thing allowed below zero")
    txn(direction="spend", amount_minor=30000, account_id=6)
    with app.app_context(), app.test_request_context():
        b = bal.balances_for_display()
        rows = {r["id"]: r for r in sql("SELECT * FROM accounts")}
        check("a card charge puts it in debt", b[6].minor == -30000)
        check("which is not flagged as overdrawn", not bal.is_overdrawn(rows[6], b[6]))
        check("cash in the black is not flagged", not bal.is_overdrawn(rows[2], b[2]))

    txn(direction="spend", amount_minor=9000, account_id=2)   # more than cash holds
    with app.app_context(), app.test_request_context():
        b = bal.balances_for_display()
        rows = {r["id"]: r for r in sql("SELECT * FROM accounts")}
        check("cash below zero is flagged", bal.is_overdrawn(rows[2], b[2]))
        check("but it was still saved — warn, never block", b[2].minor == -4000)

    print("\nforeign currency: converted where it can be, counted where it cannot")
    # A EUR charge on an EGP account, with the rate captured at entry.
    txn(direction="spend", amount_minor=10000, currency="EUR", fx_rate_to_base=54.0, account_id=1)
    with app.app_context(), app.test_request_context():
        b = bal.balances_for_display()
        check("a foreign charge on a base-currency account converts",
              b[1].minor == 99000 - 540000)
        check("and the balance is still exact", not b[1].approximate)

    # An EGP charge on the EUR account: base currency, so no rate was stored,
    # and nothing points from EGP to EUR.
    txn(direction="spend", amount_minor=5000, currency="EGP", account_id=3)
    with app.app_context(), app.test_request_context():
        b = bal.balances_for_display()
        check("a leg that cannot be converted is not guessed at", b[3].approximate)
        check("it is counted instead", b[3].unconverted == 1)
        check("and the convertible part of the balance is untouched", b[3].minor == 20200)
        rows = {r["id"]: r for r in sql("SELECT * FROM accounts")}
        check("an approximate balance is never called overdrawn",
              not bal.is_overdrawn(rows[3], b[3]))

    print("\nthe month figures respect who is looking (rule 4)")
    conn = dbmod.connect(T.DATABASE_PATH)
    conn.execute("DELETE FROM transactions")
    conn.commit()
    conn.close()
    from datetime import date
    today = date(2026, 8, 16)
    txn(user_id=1, direction="spend", amount_minor=10000, is_shared=1, occurred_on="2026-08-05")
    txn(user_id=2, direction="spend", amount_minor=20000, is_shared=0, occurred_on="2026-08-06")
    txn(user_id=2, direction="spend", amount_minor=30000, is_shared=1, occurred_on="2026-08-07")
    txn(user_id=1, direction="spend", amount_minor=99000, is_shared=1, occurred_on="2026-07-31")

    admin = {"id": 1, "role": "admin", "timezone": "Africa/Cairo", "default_shared": 1}
    member = {"id": 1, "role": "member", "timezone": "Africa/Cairo", "default_shared": 1}
    with app.app_context(), app.test_request_context():
        vs, vp = visibility_sql(admin)
        total, unconv = bal.month_spend(admin, today, vs, vp)
        check("admin sees the whole month (100+200+300)", total == 60000)
        check("and last month stays out of it", unconv == 0 and total == 60000)

        vs, vp = visibility_sql(member)
        total, _ = bal.month_spend(member, today, vs, vp)
        check("a member's month excludes another's private row (100+300)", total == 40000)

    print("\nthe breakdown groups by the parent category")
    conn = dbmod.connect(T.DATABASE_PATH)
    conn.execute("DELETE FROM transactions")
    conn.commit()
    conn.close()
    txn(direction="spend", amount_minor=10000, category_id=101, occurred_on="2026-08-05")  # Coffee
    txn(direction="spend", amount_minor=20000, category_id=102, occurred_on="2026-08-06")  # Restaurant
    txn(direction="spend", amount_minor=5000, category_id=1, occurred_on="2026-08-07")     # Groceries
    with app.app_context(), app.test_request_context():
        vs, vp = visibility_sql(admin)
        groups = bal.month_by_category(admin, today, vs, vp)
        by_name = {g["name"]: g for g in groups}
        check("Coffee and Restaurant roll up into Eating Out",
              by_name["Eating Out"]["minor"] == 30000 and by_name["Eating Out"]["count"] == 2)
        check("a top-level category stands alone", by_name["Groceries"]["minor"] == 5000)
        check("largest first", groups[0]["name"] == "Eating Out")

    print("\nediting goes through the same rules as creating")
    def login(client, username="admin"):
        r = client.get("/login")
        return client.post("/login",
                           data={"username": username, "password": "pw12345678",
                                 "_csrf": token(r.data)})

    conn = dbmod.connect(T.DATABASE_PATH)
    conn.execute("DELETE FROM transactions")
    conn.execute("UPDATE accounts SET withdrawal_limit_minor = 60000 WHERE id = 4")
    conn.commit()
    conn.close()

    with app.test_client() as c:
        login(c)
        r = c.get("/")
        c.post("/", data={"_csrf": token(r.data), "amount": "100", "merchant_id": "60",
                          "direction": "spend", "account_id": "1"})
        tid = sql("SELECT id FROM transactions ORDER BY id DESC LIMIT 1")[0]["id"]

        r = c.get(f"/transactions/{tid}/edit")
        check("the edit form loads", r.status_code == 200)
        r = c.post(f"/transactions/{tid}/edit", data={
            "_csrf": token(r.data), "amount": "-5", "currency": "EGP", "direction": "spend",
            "account_id": "1", "occurred_on": "2026-08-16"})
        check("an edit to a negative amount is refused", r.status_code == 400)
        check("with the same sentence a new entry would get", b"positive" in r.data)

        r = c.get(f"/transactions/{tid}/edit")
        r = c.post(f"/transactions/{tid}/edit", data={
            "_csrf": token(r.data), "amount": "250", "currency": "EGP", "direction": "spend",
            "account_id": "1", "occurred_on": "2026-08-16", "is_shared": "1"})
        check("a valid edit saves", r.status_code == 302)
        row = sql("SELECT amount_minor, user_id FROM transactions WHERE id = ?", (tid,))[0]
        check("the new amount is stored as minor units", row["amount_minor"] == 25000)
        check("and the owner is not reassigned by the edit", row["user_id"] == 1)

    print("\nan edit is not measured against its own old amount")
    # The card's daily withdrawal ceiling is 600.00. Withdraw 500.00, then edit
    # it to 550.00 — under the ceiling, but over it if the original still counts.
    with app.test_client() as c:
        login(c)
        r = c.get("/")
        c.post("/", data={"_csrf": token(r.data), "amount": "500", "direction": "transfer",
                          "account_id": "4", "counter_account_id": "2",
                          "occurred_on": "2026-08-16"})
        wid = sql("SELECT id FROM transactions WHERE direction='transfer' "
                  "ORDER BY id DESC LIMIT 1")[0]["id"]
        r = c.get(f"/transactions/{wid}/edit")
        r = c.post(f"/transactions/{wid}/edit", data={
            "_csrf": token(r.data), "amount": "550", "currency": "EGP", "direction": "transfer",
            "account_id": "4", "counter_account_id": "2", "occurred_on": "2026-08-16"})
        check("raising a withdrawal within the ceiling is allowed", r.status_code == 302)
        r = c.get(f"/transactions/{wid}/edit")
        r = c.post(f"/transactions/{wid}/edit", data={
            "_csrf": token(r.data), "amount": "700", "currency": "EGP", "direction": "transfer",
            "account_id": "4", "counter_account_id": "2", "occurred_on": "2026-08-16"})
        check("but past it is still refused", r.status_code == 400)

    print("\nwho may change what")
    with app.test_client() as c:
        login(c, "mem")
        tid = sql("SELECT id FROM transactions WHERE user_id = 1 ORDER BY id LIMIT 1")[0]["id"]
        check("a member cannot open someone else's entry",
              c.get(f"/transactions/{tid}/edit").status_code == 403)
        r = c.get("/transactions")
        check("but can still see it if it is shared", r.status_code == 200)
        check("the list page carries no CSRF token — delete is not reachable from it",
              token(r.data) == "")
        c.post(f"/transactions/{tid}/delete", data={"_csrf": "borrowed"})
        check("and cannot delete it",
              len(sql("SELECT 1 FROM transactions WHERE id = ?", (tid,))) == 1)

    with app.test_client() as c:
        login(c)
        r = c.get(f"/transactions/{tid}/edit")
        c.post(f"/transactions/{tid}/delete", data={"_csrf": token(r.data)})
        check("the owner can", len(sql("SELECT 1 FROM transactions WHERE id = ?", (tid,))) == 0)

    print("\nthe list filters")
    conn = dbmod.connect(T.DATABASE_PATH)
    conn.execute("DELETE FROM transactions")
    conn.commit()
    conn.close()
    txn(amount_minor=1100, merchant_id=60, category_id=101, occurred_on="2026-08-01",
        note="morning flat white")
    txn(amount_minor=2200, merchant_id=None, category_id=1, occurred_on="2026-08-02",
        account_id=2, is_online=1)
    txn(amount_minor=3300, direction="transfer", account_id=1, counter_account_id=3,
        counter_amount_minor=60, counter_currency="EUR", occurred_on="2026-08-03")

    with app.test_client() as c:
        login(c)
        get = lambda qs: c.get("/transactions?" + qs).data
        check("unfiltered shows everything", get("").count(b"list__item") >= 3)
        check("note search finds the row", b"morning flat white" in get("q=flat+white"))
        check("and excludes the others",
              b"11.00" in get("q=flat") and b"22.00" not in get("q=flat"))
        check("a parent category catches its children",
              b"11.00" in get("category_id=2"))
        check("online filter narrows", b"22.00" in get("online=1") and b"11.00" not in get("online=1"))
        check("account filter catches a transfer from either end",
              b"33.00" in get("account_id=3"))
        check("date range bounds it",
              b"22.00" in get("from=2026-08-02&to=2026-08-02")
              and b"11.00" not in get("from=2026-08-02&to=2026-08-02"))
        check("a filter with no matches says so", b"No entry matches" in get("q=zzzznothing"))
        check("the header states the window, so it cannot be read as the month total",
              b"most recent" in get(""))

    print("\ndeleting an entry asks first")
    with app.test_client() as c:
        login(c)
        before = sql("SELECT COUNT(*) FROM transactions")[0][0]

        page = c.get("/transactions").data
        edit_page = c.get("/transactions/1/edit").data
        check("the edit screen offers a link, not a button that deletes on the tap",
              b'href="/transactions/1/delete"' in edit_page)
        check("and nothing on it posts straight to delete",
              b'action="/transactions/1/delete"' not in edit_page)

        ask = c.get("/transactions/1/delete")
        check("the link leads to a question", ask.status_code == 200)
        check("which shows what is about to go rather than asking in the abstract",
              b"Delete this entry?" in ask.data and b"11.00" in ask.data)
        check("with a way out", b"Keep it" in ask.data)
        check("asking changes nothing on its own",
              sql("SELECT COUNT(*) FROM transactions")[0][0] == before)
        check("it is a page, not a confirm() the CSP would refuse to run",
              b"onclick" not in ask.data)

        c.post("/transactions/1/delete", data={"_csrf": token(ask.data)})
        check("the second tap is the one that acts",
              sql("SELECT COUNT(*) FROM transactions")[0][0] == before - 1)

        check("a question about an entry that is gone is a 404",
              c.get("/transactions/1/delete").status_code == 404)

    with app.test_client() as c:
        login(c, "mem")
        check("and someone else's entry cannot even be asked about",
              c.get("/transactions/2/delete").status_code in (403, 404))

    print("\na transfer has no merchant, so the edit screen stops offering one")
    transfer_id = None
    with app.app_context(), app.test_request_context():
        pass
    rows = sql("SELECT id FROM transactions WHERE direction = 'transfer' LIMIT 1")
    transfer_id = rows[0][0] if rows else None
    with app.test_client() as c:
        login(c)
        # Merchant and in-person/online both describe a counterparty, so they
        # travel together in one group — a transfer has neither.
        if transfer_id:
            page = c.get(f"/transactions/{transfer_id}/edit").data
            check("the merchant field is hidden on a transfer, with JavaScript off",
                  b'id="party-fields" hidden' in page)
            check("and the transfer's own pair is shown instead",
                  b'id="transfer-fields" hidden' not in page)
        spend = sql("SELECT id FROM transactions WHERE direction = 'spend' LIMIT 1")[0][0]
        page = c.get(f"/transactions/{spend}/edit").data
        check("and shown on a spend, where it means something",
              b'id="party-fields" hidden' not in page and b'id="party-fields"' in page)
        check("while a spend is not asked which account it went into",
              b'id="transfer-fields" hidden' in page)
        check("nor for a rate to a currency it is already in",
              b'id="fx-field" hidden' in page)

        js = Path("static/js/ledger-edit.js").read_text(encoding="utf-8")
        check("the script covers changing the type while the page is open",
              "syncDirection" in js and "data-when-transfer" in js)
        check("and clears an arriving amount that is about a currency it left behind",
              "counterAmount.value = \"\"" in js)
        check("the destination options carry the currency that makes that possible",
              b"data-currency" in page)

    print("\nthe list opens on you rather than on everybody")
    with app.test_client() as c:
        login(c, "mem")
        page = c.get("/transactions").data
        check("a member's list is their own by default", b"Your" in page)
        check("without that counting as a filter they applied",
              b"applied" not in page.split(b"disclosure__summary")[1][:200])
        check("and it offers the whole household as a link", b"Show everyone" in page)

        mine = c.get("/transactions").data
        everyone = c.get("/transactions?user_id=all").data
        check("the two are different lists",
              mine.count(b"list__item") < everyone.count(b"list__item"))
        check("asking for everyone does count as a filter", b"applied" in everyone)
        check("'Anyone' is a value rather than an empty option",
              b'value="all"' in page)
        check("and you can still ask for one named person",
              c.get("/transactions?user_id=1").status_code == 200)

    with app.test_client() as c:
        login(c)
        page = c.get("/transactions").data
        check("the person picker marks which one is you", b"(you)" in page)

    print("\nthe month screen answers even when nothing was spent")
    with app.test_client() as c:
        login(c)
        page = c.get("/dashboard").data
        check("a month of pure spending gets no second figure — the net would be "
              "the total above with a minus sign, said twice",
              b"Came in" not in page)

    # A ledger with income and a transfer in it and no spending at all — which is
    # what a household looks like on day one, and used to read as "nothing
    # logged this month".
    other = Path(tempfile.mkdtemp())

    class Q(Config):
        DATABASE_PATH = other / "quiet.db"
        UPLOAD_DIR = other / "uploads"
        SECRET_KEY = "test-key"
        SESSION_COOKIE_SECURE = False

    qconn = dbmod.connect(Q.DATABASE_PATH)
    migrate(qconn, Config.MIGRATIONS_DIR, log=lambda *_: None)
    qconn.execute(
        "INSERT INTO users (id, username, display_name, password_hash, role, default_shared, "
        "timezone, is_active, created_at) VALUES (1,'admin','Admin',?,'admin',1,'Africa/Cairo',"
        "1,'t')", (generate_password_hash("pw12345678"),))
    qconn.execute(
        "INSERT INTO accounts (id, name, type, currency, opening_balance_minor, is_active, "
        "sort_order, created_at) VALUES (1,'CIB','bank','EGP',0,1,10,'t'),"
        "(2,'Cash','cash','EGP',0,1,20,'t')")
    today = date.today().isoformat()
    qconn.execute(
        "INSERT INTO transactions (user_id, occurred_on, direction, amount_minor, currency, "
        "account_id, is_online, is_shared, receiptless, created_at, updated_at) VALUES "
        "(1,?,'income',500000,'EGP',1,0,1,0,'t','t'), (1,?,'income',20000,'EGP',1,0,1,0,'t','t')",
        (today, today))
    qconn.commit()
    qconn.close()

    quiet = create_app(Q)
    quiet.config.update(TESTING=True)
    with quiet.test_client() as c:
        page = c.get("/login").data
        c.post("/login", data={"username": "admin", "password": "pw12345678",
                               "_csrf": token(page)})
        page = c.get("/dashboard").data
        check("two income entries and no spending does not read as nothing happened",
              b"Nothing logged this month" not in page)
        check("it says what is actually true instead", b"No spending this month" in page)
        check("and counts what was logged", b"2 entries logged" in page)
        check("with the income total on screen", b"5,200.00" in page)
        check("income gets its own figure once there is any", b"Came in" in page)
        check("and so does the net", b"Net" in page)

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
