"""Phase 1 verification â€” the entry form and the reference data behind it.

Covers what is expensive to get wrong: money never becoming a float, the
merchant auto-fill actually firing server-side, sharing inherited from the
owner, transfers staying coherent, undo being bounded, and CSRF being real.
"""

from __future__ import annotations

import re
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import db as dbmod
import transactions as txns
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


def main() -> int:
    tmpdir = Path(tempfile.mkdtemp())

    class T(Config):
        DATABASE_PATH = tmpdir / "p1.db"
        UPLOAD_DIR = tmpdir / "uploads"
        SECRET_KEY = "test-key"
        SESSION_COOKIE_SECURE = False

    conn = dbmod.connect(T.DATABASE_PATH)
    migrate(conn, Config.MIGRATIONS_DIR, log=lambda *_: None)
    pw = generate_password_hash("pw12345678")
    conn.execute(
        "INSERT INTO users (id, username, display_name, password_hash, role, default_shared, "
        "timezone, is_active, created_at) VALUES "
        "(1,'admin','Admin',?,'admin',1,'Africa/Cairo',1,'2026-08-15T00:00:00Z'),"
        "(2,'mem','Member',?,'member',0,'Africa/Cairo',1,'2026-08-15T00:00:00Z')", (pw, pw))
    conn.execute(
        "INSERT INTO accounts (id, name, type, currency, is_active, sort_order, created_at) "
        "VALUES (1,'CIB Current','bank','EGP',1,10,'t'),"
        "       (2,'Cash','cash','EGP',1,20,'t'),"
        "       (3,'DE Giro','bank','EUR',1,30,'t'),"
        "       (4,'Vodafone Cash','wallet','EGP',1,40,'t')")
    conn.execute(
        "INSERT INTO account_owners (account_id, user_id, created_at) VALUES (3,2,'t')")
    # A debit card hanging off the bank. Cash only comes out of a card, so the
    # withdrawal tests need one, and the trigger in 003 means a card cannot be
    # inserted without its network, colour and ceiling.
    conn.execute(
        "INSERT INTO accounts (id, name, type, currency, is_active, sort_order, "
        "parent_account_id, card_network, card_color, withdrawal_limit_minor, "
        "card_expires_on, created_at) "
        "VALUES (5,'CIB Debit','debit_card','EGP',1,50,1,'Visa','#1F6F63',600000,"
        "'2099-12','t')")
    # A merchant with full defaults, to prove auto-fill happens without JavaScript.
    conn.execute(
        "INSERT INTO merchants (id, name, default_category_id, default_is_online, "
        "default_account_id, is_system, is_active, created_at) "
        "VALUES (50,'Seoudi',1,0,2,0,1,'t'), (51,'Amazon',142,1,1,0,1,'t')")
    conn.commit()
    conn.close()

    app = create_app(T)
    app.config.update(TESTING=True)

    def login(client, username="admin", password="pw12345678"):
        r = client.get("/login")
        return client.post("/login",
                           data={"username": username, "password": password, "_csrf": token(r.data)})

    def post_entry(client, **fields):
        r = client.get("/")
        fields.setdefault("_csrf", token(r.data))
        return client.post("/", data=fields)

    print("\nthe form renders what section 6 asks for")
    with app.test_client() as c:
        login(c)
        html = c.get("/").data.decode()
        check("amount input is first, autofocused, decimal keypad",
              'inputmode="decimal"' in html and "autofocus" in html
              and html.index('id="amount"') < html.index('id="merchant-search"'))
        # Migration 004 split the old Receipt-less merchant in two: whether a
        # receipt exists is its own toggle, and naming nobody is its own chip.
        check("the receipt toggle is a chip of its own, not a merchant",
              'name="receiptless"' in html and "No receipt" in html)
        check("it is not in the merchant radiogroup",
              html.index('name="receiptless"') < html.index('role="radiogroup" aria-label="Merchant"'))
        check("clearing the merchant is pinned first",
              html.index('id="m-none"') < html.index("Seoudi"))
        check("chips are radio inputs, so they work without JavaScript",
              'name="merchant_id"' in html and 'type="radio"' in html)
        check("merchant defaults ride along as data attributes for instant fill",
              'data-category="1"' in html and 'data-account="2"' in html)
        check("date, currency and sharing are pre-filled and collapsed",
              "<details" in html and 'name="occurred_on"' in html)
        check("the camera seat exists with no dead control in it",
              "pos__aux" in html and "camera" in html.lower())
        check("amounts use tabular figures", 'class="pos__amount num"' in html)

    print("\nthe ten-second path")
    with app.test_client() as c:
        login(c)
        r = post_entry(c, amount="47.50", merchant_id="50", direction="spend")
        check("type amount, tap chip, tap save â†’ redirect", r.status_code == 302)
        check("lands back on the form, not a detail page", r.headers["Location"].startswith("/?saved="))

        html = c.get(r.headers["Location"]).data.decode()
        check("toast confirms the amount", "47.50" in html)
        check("toast names the merchant", "Seoudi" in html)
        check("toast offers undo", "Undo" in html)

    print("\nmoney is stored as integers")
    conn = dbmod.connect(T.DATABASE_PATH)
    row = conn.execute("SELECT amount_minor, currency, is_shared, user_id, direction, "
                       "occurred_on FROM transactions ORDER BY id DESC LIMIT 1").fetchone()
    check("47.50 EGP stored as 4750 minor units", row["amount_minor"] == 4750)
    check("stored as INTEGER, not REAL or TEXT", isinstance(row["amount_minor"], int))
    check("currency recorded", row["currency"] == "EGP")
    check("occurred_on is a local calendar date", re.fullmatch(r"\d{4}-\d{2}-\d{2}", row["occurred_on"]))
    conn.close()

    print("\nmerchant defaults auto-fill server-side (JavaScript off)")
    with app.test_client() as c:
        login(c)
        post_entry(c, amount="120", merchant_id="50", direction="spend")
        conn = dbmod.connect(T.DATABASE_PATH)
        row = conn.execute("SELECT category_id, account_id, is_online FROM transactions "
                           "ORDER BY id DESC LIMIT 1").fetchone()
        conn.close()
        check("category filled from the merchant", row["category_id"] == 1)
        check("account filled from the merchant", row["account_id"] == 2)
        check("online flag filled from the merchant", row["is_online"] == 0)

    with app.test_client() as c:
        login(c)
        post_entry(c, amount="99", merchant_id="51", direction="spend")
        conn = dbmod.connect(T.DATABASE_PATH)
        row = conn.execute("SELECT category_id, account_id, is_online FROM transactions "
                           "ORDER BY id DESC LIMIT 1").fetchone()
        conn.close()
        check("an online merchant sets the online flag", row["is_online"] == 1)
        check("and its own category/account", (row["category_id"], row["account_id"]) == (142, 1))

    print("\nexplicit choices beat merchant defaults")
    with app.test_client() as c:
        login(c)
        post_entry(c, amount="15", merchant_id="50", category_id="2", account_id="1")
        conn = dbmod.connect(T.DATABASE_PATH)
        row = conn.execute("SELECT category_id, account_id FROM transactions "
                           "ORDER BY id DESC LIMIT 1").fetchone()
        conn.close()
        check("a chosen category is not overwritten", row["category_id"] == 2)
        check("a chosen account is not overwritten", row["account_id"] == 1)

    print("\nsharing follows the owner's default (section 4)")
    with app.test_client() as c:
        login(c)  # admin, default_shared = 1
        post_entry(c, amount="10", merchant_id="50")
        conn = dbmod.connect(T.DATABASE_PATH)
        check("admin's entry inherits shared = 1",
              conn.execute("SELECT is_shared FROM transactions ORDER BY id DESC LIMIT 1")
                  .fetchone()["is_shared"] == 1)
        conn.close()

    with app.test_client() as c:
        login(c, "mem")  # default_shared = 0
        post_entry(c, amount="10", merchant_id="50")
        conn = dbmod.connect(T.DATABASE_PATH)
        r2 = conn.execute("SELECT is_shared, user_id FROM transactions ORDER BY id DESC LIMIT 1").fetchone()
        conn.close()
        check("member's entry inherits shared = 0", r2["is_shared"] == 0)
        check("and is owned by the member", r2["user_id"] == 2)

    with app.test_client() as c:
        login(c, "mem")
        post_entry(c, amount="10", merchant_id="50", is_shared="1")
        conn = dbmod.connect(T.DATABASE_PATH)
        check("an individual entry can be flipped shared",
              conn.execute("SELECT is_shared FROM transactions ORDER BY id DESC LIMIT 1")
                  .fetchone()["is_shared"] == 1)
        conn.close()

    print("\ninline merchant add")
    with app.test_client() as c:
        login(c)
        r = c.get("/")
        r = c.post("/entry/merchants", data={"name": "Gad", "_csrf": token(r.data)})
        check("inline add returns the new merchant", r.status_code == 200 and r.json["name"] == "Gad")
        first_id = r.json["id"]
        r2 = c.get("/")
        r2 = c.post("/entry/merchants", data={"name": "gad", "_csrf": token(r2.data)})
        check("a case-variant re-uses the same merchant", r2.json["id"] == first_id)

    with app.test_client() as c:
        login(c)
        post_entry(c, amount="30", new_merchant_name="Kazyon")
        conn = dbmod.connect(T.DATABASE_PATH)
        check("no-JavaScript path creates the merchant on save",
              conn.execute("SELECT COUNT(*) n FROM merchants WHERE name='Kazyon'").fetchone()["n"] == 1)
        check("and attaches it to the transaction",
              conn.execute(
                  "SELECT m.name FROM transactions t JOIN merchants m ON m.id = t.merchant_id "
                  "ORDER BY t.id DESC LIMIT 1").fetchone()["name"] == "Kazyon")
        conn.close()

    print("\nrecent merchants become chips")
    with app.test_client() as c:
        login(c)
        html = c.get("/").data.decode()
        chips = html.split('id="more"')[0]
        check("a merchant used moments ago is now a chip", "Kazyon" in chips)
        check("the way to clear a selection still leads",
              chips.index("No merchant") < chips.index("Kazyon"))

    print("\ntransfers")
    with app.test_client() as c:
        login(c)
        # Bank to wallet. Cash is deliberately not a transfer target from here â€”
        # that is a withdrawal and belongs to a card (see verify_accounts.py).
        post_entry(c, amount="500", direction="transfer", account_id="1", counter_account_id="4")
        conn = dbmod.connect(T.DATABASE_PATH)
        row = conn.execute("SELECT * FROM transactions WHERE direction='transfer' "
                           "ORDER BY id DESC LIMIT 1").fetchone()
        conn.close()
        check("same-currency transfer copies the amount across",
              row["counter_amount_minor"] == 50000 and row["counter_currency"] == "EGP")
        check("a transfer carries no merchant", row["merchant_id"] is None)

    with app.test_client() as c:
        login(c)
        r = post_entry(c, amount="100", direction="transfer", account_id="3",
                       counter_account_id="1", currency="EUR", fx_rate_to_base="54.2",
                       counter_amount="5420")
        check("cross-currency transfer accepted", r.status_code == 302)
        conn = dbmod.connect(T.DATABASE_PATH)
        row = conn.execute("SELECT * FROM transactions WHERE currency='EUR' "
                           "ORDER BY id DESC LIMIT 1").fetchone()
        conn.close()
        check("both sides recorded (100 EUR out, 5420 EGP in)",
              row["amount_minor"] == 10000 and row["counter_amount_minor"] == 542000)
        check("counter currency recorded", row["counter_currency"] == "EGP")
        check("the day's fx rate captured", abs(row["fx_rate_to_base"] - 54.2) < 1e-9)

    with app.test_client() as c:
        login(c)
        r = post_entry(c, amount="100", direction="transfer", account_id="3",
                       counter_account_id="1", currency="EUR", fx_rate_to_base="54.2")
        check("cross-currency transfer without the arriving amount is refused",
              r.status_code == 400 and b"arrived" in r.data)
        r = post_entry(c, amount="50", direction="transfer", account_id="1", counter_account_id="1")
        check("transfer into the same account is refused", r.status_code == 400)

    print("\nvalidation keeps what was typed")
    with app.test_client() as c:
        login(c)
        r = post_entry(c, amount="-5", merchant_id="50")
        check("negative amount refused", r.status_code == 400)
        check("with a sentence, not a stack trace", b"positive" in r.data)
        r = post_entry(c, amount="abc", merchant_id="50", note="dinner with mum")
        check("gibberish refused", r.status_code == 400)
        check("the note typed is still in the form", b"dinner with mum" in r.data)
        r = post_entry(c, amount="0", merchant_id="50")
        check("zero refused", r.status_code == 400)
        r = post_entry(c, amount="10", occurred_on="2099-01-01", merchant_id="50")
        check("a date in the future refused", r.status_code == 400)
        r = post_entry(c, amount="10", currency="EUR", account_id="1", merchant_id="50")
        check("foreign currency without a rate refused", r.status_code == 400)

    print("\nundo")
    with app.test_client() as c:
        login(c)
        r = post_entry(c, amount="77", merchant_id="50")
        txn_id = int(r.headers["Location"].split("=")[1])
        page = c.get("/")
        r = c.post(f"/entry/{txn_id}/undo", data={"_csrf": token(page.data)})
        check("undo removes the entry", r.status_code == 302)
        conn = dbmod.connect(T.DATABASE_PATH)
        check("it is gone from the database",
              conn.execute("SELECT COUNT(*) n FROM transactions WHERE id=?", (txn_id,))
                  .fetchone()["n"] == 0)
        conn.close()

    with app.test_client() as c:
        login(c)
        r = post_entry(c, amount="88", merchant_id="50")
        txn_id = int(r.headers["Location"].split("=")[1])
    with app.test_client() as c2:
        login(c2, "mem")
        page = c2.get("/")
        c2.post(f"/entry/{txn_id}/undo", data={"_csrf": token(page.data)})
        conn = dbmod.connect(T.DATABASE_PATH)
        check("someone else cannot undo your entry",
              conn.execute("SELECT COUNT(*) n FROM transactions WHERE id=?", (txn_id,))
                  .fetchone()["n"] == 1)
        conn.close()

    print("\nCSRF")
    with app.test_client() as c:
        login(c)
        check("entry POST without a token is rejected",
              c.post("/", data={"amount": "10"}).status_code == 400)
        check("inline merchant add without a token is rejected",
              c.post("/entry/merchants", data={"name": "X"}).status_code == 400)
        check("a wrong token is rejected",
              c.post("/", data={"amount": "10", "_csrf": "nope"}).status_code == 400)

    print("\nreference data is admin-only")
    with app.test_client() as c:
        login(c, "mem")
        check("a member cannot reach setup", c.get("/settings/").status_code == 403)
        check("nor the accounts screen", c.get("/settings/accounts").status_code == 403)
        check("but can still log", c.get("/").status_code == 200)

    print("\naccounts CRUD")
    with app.test_client() as c:
        login(c)
        page = c.get("/settings/accounts/new")
        r = c.post("/settings/accounts/new", data={
            "name": "NBE Visa", "type": "credit_card", "currency": "EGP",
            "opening_balance": "-1,250.75", "sort_order": "5", "is_active": "1",
            # A card carries its network, colour and ceilings or it does not
            # save â€” see verify_accounts.py for each of those on its own.
            "card_network": "Visa", "card_color": "#0F1E24", "card_expires_on": "2099-12",
            "withdrawal_limit": "6000", "credit_limit_local": "50000",
            "credit_limit_intl": "20000",
            "_csrf": token(page.data)})
        check("account created", r.status_code == 302)
        conn = dbmod.connect(T.DATABASE_PATH)
        row = conn.execute("SELECT * FROM accounts WHERE name='NBE Visa'").fetchone()
        conn.close()
        check("a credit card may open in debt", row["opening_balance_minor"] == -125075)
        check("stored as an integer", isinstance(row["opening_balance_minor"], int))

        page = c.get("/settings/accounts/new")
        r = c.post("/settings/accounts/new", data={
            "name": "nbe visa", "type": "bank", "currency": "EGP", "_csrf": token(page.data)})
        check("a case-variant duplicate name is refused", r.status_code == 400)

    print("\ncategories stay one level deep")
    with app.test_client() as c:
        login(c)
        page = c.get("/settings/categories/new")
        r = c.post("/settings/categories/new", data={
            "name": "Too deep", "parent_id": "101", "_csrf": token(page.data)})
        check("a third level is refused with a sentence",
              r.status_code == 400 and b"one level" in r.data)

    print("\nwho was paid and whether there is a receipt are independent")
    with app.test_client() as c:
        login(c)

        def last():
            conn = dbmod.connect(T.DATABASE_PATH)
            try:
                return conn.execute(
                    "SELECT merchant_id, receiptless FROM transactions "
                    "ORDER BY id DESC LIMIT 1").fetchone()
            finally:
                conn.close()

        post_entry(c, amount="15", merchant_id="50", receiptless="1", account_id="1")
        row = last()
        check("a named shop that hands over nothing",
              row["merchant_id"] == 50 and row["receiptless"] == 1)

        post_entry(c, amount="16", merchant_id="", account_id="1")
        row = last()
        check("a nameless stall that does print a slip",
              row["merchant_id"] is None and row["receiptless"] == 0)

        post_entry(c, amount="17", merchant_id="", receiptless="1", account_id="1")
        row = last()
        check("and the street vendor who is neither",
              row["merchant_id"] is None and row["receiptless"] == 1)

    print("\nthe account box names an account, never 'Auto'")
    with app.test_client() as c:
        login(c)
        page = c.get("/").data
        select = page.split(b'id="account_id"')[1].split(b"</select>")[0]
        check("there is no blank option to hide the real answer behind",
              b'value=""' not in select)
        check("one account is selected when the page arrives", b"selected" in select)
        check("and the form says which one it is by naming it, not by saying Auto",
              b">Auto<" not in select)
        check("category keeps its Auto — 'no category' is a state a row may stay in",
              b'value=""' in page.split(b'id="category_id"')[1].split(b"</select>")[0])

        # Removing the blank option is only safe because the script stopped
        # using emptiness to mean "untouched". If it ever goes back to that,
        # tapping a merchant silently stops switching the account.
        js = Path("static/js/entry.js").read_text(encoding="utf-8")
        check("the script tracks a hand-picked account explicitly",
              "dataset.touched" in js)
        check("rather than by looking for an empty value it can no longer see",
              "!account.value" not in js)
        check("and the box carries the default to fall back to",
              b'data-default=' in select)

    print("\ncross-currency transfers have to be able to be the same money")
    with app.app_context(), app.test_request_context():
        dbmod.execute(
            "INSERT OR REPLACE INTO fx_rates (base, currency, rate_to_base, fetched_at, source) "
            "VALUES ('EGP','EUR',55.0,'2026-08-16T00:00:00Z','test')")

    user = {"id": 1, "timezone": "Africa/Cairo", "default_shared": 1, "role": "admin"}
    move = {"direction": "transfer", "account_id": "1", "currency": "EGP",
            "counter_account_id": "3", "occurred_on": "2026-08-16"}

    with app.app_context(), app.test_request_context():
        # The one that shipped: 10.00 EGP left a bank account and 10.00 EUR
        # arrived in another. Both numbers valid, both currencies right, wrong
        # by a factor of fifty-five, and nothing anywhere objected.
        try:
            txns.create_transaction(user, {**move, "amount": "10", "counter_amount": "10"})
            check("10 EGP cannot arrive as 10 EUR", False)
        except txns.EntryError as exc:
            check("10 EGP cannot arrive as 10 EUR", True)
            check("and the message says how far out it is, not 'invalid'",
                  "factor" in str(exc) and exc.field == "counter_amount")

        # What the same transfer actually looks like.
        txns.create_transaction(user, {**move, "amount": "550", "counter_amount": "10"})
        check("550 EGP arriving as 10 EUR is fine", True)

        # A bank's rate is not the mid-market one — spread, fees, a bad day.
        # The guard has to stay out of the way of every one of those.
        txns.create_transaction(user, {**move, "amount": "550", "counter_amount": "9"})
        check("a poor rate is still a real transfer, not an error", True)
        txns.create_transaction(user, {**move, "amount": "550", "counter_amount": "12"})
        check("so is a good one", True)

        # Same currency both ends: nothing to compare, and the server copies the
        # amount across rather than asking twice.
        txns.create_transaction(user, {"direction": "transfer", "account_id": "1",
                                       "currency": "EGP", "counter_account_id": "4",
                                       "occurred_on": "2026-08-16", "amount": "25"})
        landed = dbmod.query_one(
            "SELECT counter_amount_minor FROM transactions ORDER BY id DESC LIMIT 1")
        check("a same-currency transfer still copies the amount across",
              landed["counter_amount_minor"] == 2500)

    with app.app_context(), app.test_request_context():
        # No cached rate means nothing to compare against, and a guard that
        # guesses is worse than one that stays quiet.
        dbmod.execute("DELETE FROM fx_rates")
        txns.create_transaction(user, {**move, "amount": "10", "counter_amount": "10"})
        check("with no cached rate it declines to judge rather than inventing one", True)

    print("\nthe confirmation gets out of the way")
    with app.test_client() as c:
        login(c)
        page = c.get("/").data
        made = txns_id = None
        r = c.post("/", data={
            "_csrf": token(page), "amount": "12", "direction": "spend",
            "account_id": "1", "currency": "EGP", "occurred_on": "2026-08-16"})
        saved = r.headers["Location"].split("saved=")[1]
        page = c.get(f"/?saved={saved}").data

        check("the toast appears after a save", b'id="toast"' in page)
        check("with Undo on it", b"Undo" in page)
        check("and a way to dismiss it that needs no JavaScript",
              b'class="toast__close"' in page)
        check("which is a plain link back to a clean form",
              b'class="toast__close" href="/"' in page)
        check("so dismissing it leaves no toast behind",
              b'id="toast"' not in c.get("/").data)

        js = Path("static/js/entry.js").read_text(encoding="utf-8")
        check("and it retires itself rather than sitting over the screen",
              "toast--gone" in js)
        check("after seconds rather than minutes", "12000" in js)
        check("unless you are reaching for it", "pointerenter" in js)
        css = Path("static/css/app.css").read_text(encoding="utf-8")
        check("the exit is animated, and skipped for anyone who asked it to be",
              ".toast--gone" in css and "prefers-reduced-motion" in css)

        # It leaving is the shortcut expiring, not the entry setting.
        page = c.get(f"/?saved={saved}").data
        c.post(f"/entry/{saved}/undo", data={"_csrf": token(page)})
        check("Undo still removes the entry once the toast has been dismissed",
              dbmod.query_one("SELECT COUNT(*) AS n FROM transactions "
                              "WHERE id = ?", (saved,))["n"] == 0)

    print("\nthe three directions are three colours")
    css = Path("static/css/app.css").read_text(encoding="utf-8")
    shades = {d: css.split(f".amt--{d} {{")[1].split("}")[0] for d in
              ("spend", "income", "transfer")}
    check("spend, income and transfer each have one", len(shades) == 3)
    check("and no two of them are the same",
          len({v.strip() for v in shades.values()}) == 3)
    check("transfer is a colour rather than grey — grey read as disabled once "
          "spend went red", "--text-muted" not in shades["transfer"])
    check("each is a palette variable, so dark mode gets its own",
          all("var(--" in v for v in shades.values()))
    check("and the palette defines the new one in both schemes",
          css.count("--move:") == 2)

    # One of each, so the check is about the list rather than about whatever
    # the fixture happened to leave behind.
    user = {"id": 1, "timezone": "Africa/Cairo", "default_shared": 1, "role": "admin"}
    with app.app_context(), app.test_request_context():
        txns.create_transaction(user, {"amount": "5", "direction": "spend",
                                       "account_id": "1", "occurred_on": "2026-08-16"})
        txns.create_transaction(user, {"amount": "6", "direction": "income",
                                       "account_id": "1", "occurred_on": "2026-08-16"})
        txns.create_transaction(user, {"amount": "7", "direction": "transfer",
                                       "account_id": "1", "counter_account_id": "4",
                                       "occurred_on": "2026-08-16"})

    with app.test_client() as c:
        login(c)
        page = c.get("/transactions?user_id=all").data
        check("the entry list paints all three",
              b"amt--spend" in page and b"amt--income" in page
              and b"amt--transfer" in page)

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
