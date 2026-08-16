"""Verification for account links, card details, cash rules and merchant kinds.

Everything here is a rule that is cheap to state and expensive to get wrong six
months in: a debit card that forgot which bank it draws on, a wallet that went
quietly negative, a cash withdrawal recorded against an account that cannot
dispense cash, or an employer sitting in the chip row at a till.

The browser-side behaviour these rules pair with — tapping a chip twice to clear
it, hiding the merchant block on a transfer — is checked here only as far as the
markup contract goes: that the empty option exists, and that the pieces carry
the attributes the script keys off. The tapping itself needs a browser.
"""

from __future__ import annotations

import re
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import db as dbmod
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
        DATABASE_PATH = tmpdir / "accounts.db"
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
    conn.execute(
        "INSERT INTO accounts (id, name, type, currency, owner_id, is_active, sort_order, created_at) "
        "VALUES (1,'CIB Current','bank','EGP',NULL,1,10,'t'),"
        "       (2,'Cash','cash','EGP',NULL,1,20,'t'),"
        "       (3,'Vodafone Cash','wallet','EGP',NULL,1,30,'t'),"
        "       (4,'DE Giro','bank','EUR',NULL,1,40,'t')")
    conn.execute(
        "INSERT INTO merchants (id, name, kind, default_is_online, is_system, is_active, created_at) "
        "VALUES (60,'Seoudi','spend',0,0,1,'t'), (61,'Acme Payroll','income',0,0,1,'t')")
    conn.commit()
    conn.close()

    app = create_app(T)
    app.config.update(TESTING=True)

    def login(client, username="admin", password="pw12345678"):
        r = client.get("/login")
        return client.post("/login",
                           data={"username": username, "password": password, "_csrf": token(r.data)})

    def post_account(client, account_id=None, **fields):
        url = f"/settings/accounts/{account_id}" if account_id else "/settings/accounts/new"
        page = client.get(url if account_id else "/settings/accounts/new")
        fields.setdefault("_csrf", token(page.data))
        fields.setdefault("is_active", "1")
        return client.post(url, data=fields)

    def post_entry(client, **fields):
        r = client.get("/")
        fields.setdefault("_csrf", token(r.data))
        return client.post("/", data=fields)

    def sql(query, params=()):
        conn = dbmod.connect(T.DATABASE_PATH)
        try:
            return conn.execute(query, params).fetchone()
        finally:
            conn.close()

    # The fields every card now needs. Expiry is far enough out that these tests
    # do not start failing on a calendar boundary.
    card = dict(card_network="Visa", card_color="#1F6F63", withdrawal_limit="6000",
                card_expires_on="2099-12")

    # ------------------------------------------------------------ links
    print("\naccounts link one level deep, to a bank or a wallet")
    with app.test_client() as c:
        login(c)

        r = post_account(c, name="Loose Debit", type="debit_card", currency="EGP", **card)
        check("a debit card cannot exist without the account it draws on",
              r.status_code == 400 and b"linked" in r.data)

        r = post_account(c, name="CIB Debit", type="debit_card", currency="EGP",
                         parent_account_id="1", **card)
        check("a debit card links to a bank", r.status_code == 302)
        debit = sql("SELECT * FROM accounts WHERE name='CIB Debit'")
        check("and records which account it draws on", debit["parent_account_id"] == 1)

        r = post_account(c, name="CIB Debit Two", type="debit_card", currency="EGP",
                         parent_account_id="1", **card)
        check("a second debit card on the same account is fine", r.status_code == 302)

        r = post_account(c, name="Sam", type="instapay", currency="EGP",
                         instapay_handle="@sam_pay", parent_account_id="1")
        check("an instapay handle links to the same bank", r.status_code == 302)
        check("and the name is the person and the handle, merged",
              sql("SELECT name FROM accounts WHERE instapay_handle='@sam_pay'")
                  ["name"] == "Sam - @sam_pay")

        r = post_account(c, name="Nameless", type="instapay", currency="EGP",
                         parent_account_id="1")
        check("an instapay account without a handle is refused",
              r.status_code == 400 and b"needs a handle" in r.data)

        r = post_account(c, name="Sara", type="instapay", currency="EGP",
                         instapay_handle="sara_pay", parent_account_id="3")
        check("a missing @ is added rather than argued about",
              r.status_code == 302
              and sql("SELECT name FROM accounts WHERE instapay_handle='@sara_pay'")
                  ["name"] == "Sara - @sara_pay")

        r = post_account(c, name="Bad", type="instapay", currency="EGP",
                         instapay_handle="@not a handle!", parent_account_id="1")
        check("a handle with characters Instapay does not use is refused",
              r.status_code == 400 and b"characters Instapay does not use" in r.data)

        r = post_account(c, name="Second", type="instapay", currency="EGP",
                         instapay_handle="@second_pay", parent_account_id="1")
        check("but only one instapay handle per account",
              r.status_code == 400 and b"Instapay handle" in r.data)

        r = post_account(c, name="Card on a card", type="debit_card", currency="EGP",
                         parent_account_id=str(debit["id"]), **card)
        check("a card cannot hang off another card",
              r.status_code == 400 and b"bank account or a wallet" in r.data)

        check("a wallet can hold an instapay handle too",
              sql("SELECT parent_account_id FROM accounts WHERE instapay_handle='@sara_pay'")
                  ["parent_account_id"] == 3)

        r = post_account(c, name="Pocket", type="cash", currency="EGP", parent_account_id="1")
        check("cash is linked to nothing, whatever the form says",
              r.status_code == 302 and sql("SELECT * FROM accounts WHERE name='Pocket'")
              ["parent_account_id"] is None)

        r = post_account(c, name="AmEx Gold", type="credit_card", currency="EGP",
                         parent_account_id="1", credit_limit_local="50000",
                         credit_limit_intl="20000", **card)
        check("a credit card is its own thing and takes no link",
              r.status_code == 302 and sql("SELECT * FROM accounts WHERE name='AmEx Gold'")
              ["parent_account_id"] is None)

    print("\ninstapay is the account behind it, not a second one")
    with app.test_client() as c:
        login(c)
        r = post_account(c, name="Giro", type="instapay", currency="EGP",
                         instapay_handle="@giro_pay", parent_account_id="4")
        handle = sql("SELECT * FROM accounts WHERE instapay_handle='@giro_pay'")
        check("it takes the linked account's currency, not the one posted",
              r.status_code == 302 and handle["currency"] == "EUR")

        # A wallet with no handle yet, so the refusal below is about the balance
        # rather than about the one-instapay-per-account rule.
        post_account(c, name="Aman Wallet", type="wallet", currency="EGP")
        free = sql("SELECT id FROM accounts WHERE name='Aman Wallet'")["id"]
        r = post_account(c, name="Rich", type="instapay", currency="EGP",
                         instapay_handle="@rich_pay",
                         parent_account_id=str(free), opening_balance="500")
        check("and carries no opening balance of its own",
              r.status_code == 400 and b"opening balance of its own" in r.data)

        # Changing the parent's currency has to reach the handle, or the pair
        # ends up describing two different pots.
        post_account(c, account_id=4, name="DE Giro", type="bank", currency="USD")
        check("a currency change on the account reaches its instapay handle",
              sql("SELECT currency FROM accounts WHERE instapay_handle='@giro_pay'")
                  ["currency"] == "USD")

    # -------------------------------------------------- opening balance
    print("\nonly a credit card may go below zero")
    with app.test_client() as c:
        login(c)
        r = post_account(c, name="Overdrawn Bank", type="bank", currency="EGP",
                         opening_balance="-100")
        check("a bank account cannot open in debt",
              r.status_code == 400 and b"credit card" in r.data)

        r = post_account(c, name="Overdrawn Wallet", type="wallet", currency="EGP",
                         opening_balance="-50")
        check("nor a wallet", r.status_code == 400)

        r = post_account(c, name="Overdrawn Cash", type="cash", currency="EGP",
                         opening_balance="-5")
        check("nor cash", r.status_code == 400)

        r = post_account(c, name="NBE Titanium", type="credit_card", currency="EGP",
                         opening_balance="-1,250.75", credit_limit_local="50000",
                         credit_limit_intl="20000", **card)
        check("a credit card still may, and stores it as an integer",
              r.status_code == 302
              and sql("SELECT * FROM accounts WHERE name='NBE Titanium'")
                  ["opening_balance_minor"] == -125075)

    print("\nthe database refuses it too, not just the form")
    conn = dbmod.connect(T.DATABASE_PATH)
    try:
        conn.execute(
            "INSERT INTO accounts (name, type, currency, opening_balance_minor, created_at) "
            "VALUES ('Sneaky','bank','EGP',-1,'t')")
        check("a negative opening balance inserted directly is rejected", False)
    except Exception as exc:
        check("a negative opening balance inserted directly is rejected",
              "only a credit card" in str(exc))
    finally:
        conn.close()

    # ------------------------------------------------------ card fields
    print("\ncards carry a network, a colour and a withdrawal ceiling")
    with app.test_client() as c:
        login(c)
        base = dict(type="debit_card", currency="EGP", parent_account_id="1",
                    card_expires_on="2099-12")

        r = post_account(c, name="No Network", card_color="#123456",
                         withdrawal_limit="5000", **base)
        check("network is required", r.status_code == 400 and b"network" in r.data)

        r = post_account(c, name="No Colour", card_network="Visa",
                         withdrawal_limit="5000", **base)
        check("colour is required", r.status_code == 400 and b"colour" in r.data)

        r = post_account(c, name="Bad Colour", card_network="Visa",
                         card_color="url(javascript:alert(1))", withdrawal_limit="5000", **base)
        check("a colour that is not a colour is refused",
              r.status_code == 400 and b"colour" in r.data)

        r = post_account(c, name="No Ceiling", card_network="Visa", card_color="teal", **base)
        check("a cash withdrawal limit is required",
              r.status_code == 400 and b"withdrawal limit" in r.data)

        r = post_account(c, name="Named Colour", card_network="Meeza", card_color="teal",
                         withdrawal_limit="4,000", **base)
        check("a plain colour name is accepted",
              r.status_code == 302
              and sql("SELECT * FROM accounts WHERE name='Named Colour'")
                  ["withdrawal_limit_minor"] == 400000)

    print("\ncredit cards carry two limits, and a tick for when they match")
    with app.test_client() as c:
        login(c)
        base = dict(type="credit_card", currency="EGP", **card)

        r = post_account(c, name="One Limit", credit_limit_local="50000", **base)
        check("the international limit is not optional",
              r.status_code == 400 and b"international limit is required" in r.data.lower())

        r = post_account(c, name="Two Limits", credit_limit_local="50000",
                         credit_limit_intl="20000", **base)
        row = sql("SELECT * FROM accounts WHERE name='Two Limits'")
        check("both are stored separately, in minor units",
              r.status_code == 302
              and (row["credit_limit_local_minor"], row["credit_limit_intl_minor"])
                  == (5000000, 2000000))

        r = post_account(c, name="Same Limits", credit_limit_local="30000",
                         same_limits="1", **base)
        row = sql("SELECT * FROM accounts WHERE name='Same Limits'")
        check("ticking 'the same' copies the local limit across",
              row["credit_limit_intl_minor"] == 3000000)

        # The form does not ask a debit card for a credit line, so a posted one
        # is dropped rather than argued with — there is nothing for the person
        # to correct on a field they never saw.
        r = post_account(c, name="Debit With Credit", type="debit_card", currency="EGP",
                         parent_account_id="1", credit_limit_local="5000", **card)
        row = sql("SELECT * FROM accounts WHERE name='Debit With Credit'")
        check("a credit line posted at a debit card is dropped, not stored",
              r.status_code == 302 and row["credit_limit_local_minor"] is None)

    conn = dbmod.connect(T.DATABASE_PATH)
    try:
        conn.execute(
            "INSERT INTO accounts (name, type, currency, parent_account_id, card_network, "
            "card_color, withdrawal_limit_minor, credit_limit_local_minor, card_expires_on, "
            "created_at) "
            "VALUES ('Sneaky Debit','debit_card','EGP',1,'Visa','teal',100000,50000,'2099-12','t')")
        check("and the database refuses one written directly", False)
    except Exception as exc:
        check("and the database refuses one written directly",
              "only a credit card carries credit limits" in str(exc))
    finally:
        conn.close()

    # ------------------------------------------------------ cash rules
    print("\ncash only spends, receives and is reimbursed")
    with app.test_client() as c:
        login(c)
        r = post_entry(c, amount="40", account_id="2", merchant_id="60", direction="spend")
        check("cash can be spent", r.status_code == 302)

        r = post_entry(c, amount="40", account_id="2", merchant_id="61", direction="income")
        check("cash can receive income", r.status_code == 302)

        r = post_entry(c, amount="40", account_id="2", direction="transfer",
                       counter_account_id="1")
        check("cash cannot be transferred out of",
              r.status_code == 400 and b"Cash only records" in r.data)

    print("\ncash comes out of a card, and the card is the record")
    with app.test_client() as c:
        login(c)
        debit_id = sql("SELECT id FROM accounts WHERE name='CIB Debit'")["id"]

        r = post_entry(c, amount="500", account_id="1", direction="transfer",
                       counter_account_id="2")
        check("a bank account cannot dispense cash",
              r.status_code == 400 and b"cannot dispense cash" in r.data)
        check("and it names the cards it could have been",
              b"CIB Debit" in r.data)

        insta_id = sql("SELECT id FROM accounts WHERE instapay_handle='@sam_pay'")["id"]
        r = post_entry(c, amount="500", account_id=str(insta_id), direction="transfer",
                       counter_account_id="2")
        check("nor can instapay, which offers the parent's cards instead",
              r.status_code == 400 and b"CIB Debit" in r.data)

        wallet_insta = sql("SELECT id FROM accounts WHERE instapay_handle='@sara_pay'")["id"]
        r = post_entry(c, amount="100", account_id=str(wallet_insta), direction="transfer",
                       counter_account_id="2")
        check("with no card linked at all, it says to link one",
              r.status_code == 400 and b"Link a debit card" in r.data)

        r = post_entry(c, amount="500", account_id=str(debit_id), direction="transfer",
                       counter_account_id="2")
        check("a card withdrawal is accepted", r.status_code == 302)
        row = sql("SELECT * FROM transactions WHERE direction='transfer' ORDER BY id DESC LIMIT 1")
        check("recorded against the card, landing in cash",
              row["account_id"] == debit_id and row["counter_account_id"] == 2)
        check("both sides carry the amount", row["counter_amount_minor"] == 50000)

        r = post_entry(c, amount="9000", account_id=str(debit_id), direction="transfer",
                       counter_account_id="2")
        check("a withdrawal over the card's ceiling is refused",
              r.status_code == 400 and b"withdrawal limit" in r.data)

    # -------------------------------------------------- merchant kinds
    print("\nspending and income keep separate lists")
    with app.test_client() as c:
        login(c)
        html = c.get("/").data.decode()
        check("every chip declares which side it belongs to", 'data-kind="spend"' in html)
        check("income sources are marked as such", 'data-kind="income"' in html)
        check("Receipt-less belongs to both", 'data-kind="both"' in html)

        r = post_entry(c, amount="20", merchant_id="61", direction="spend")
        check("an income source cannot be spent at",
              r.status_code == 400 and b"income source" in r.data)

        r = post_entry(c, amount="20", merchant_id="60", direction="income")
        check("and a shop is not somewhere income comes from",
              r.status_code == 400 and b"spend at" in r.data)

        r = post_entry(c, amount="5000", merchant_id="61", direction="income", account_id="1")
        check("income from an income source is fine", r.status_code == 302)

    with app.test_client() as c:
        login(c)
        page = c.get("/")
        r = c.post("/entry/merchants",
                   data={"name": "New Employer", "direction": "income", "_csrf": token(page.data)})
        check("a merchant added while logging income joins the income list",
              r.status_code == 200
              and sql("SELECT kind FROM merchants WHERE name='New Employer'")["kind"] == "income")

        page = c.get("/")
        r = c.post("/entry/merchants",
                   data={"name": "New Shop", "direction": "spend", "_csrf": token(page.data)})
        check("and one added while spending joins the spending list",
              sql("SELECT kind FROM merchants WHERE name='New Shop'")["kind"] == "spend")

    # --------------------------------------------- entry form contract
    print("\nthe entry form's merchant controls")
    with app.test_client() as c:
        login(c)
        html = c.get("/").data.decode()
        check("there is one merchant text box, not two",
              html.count('name="new_merchant_name"') == 1)
        check("and it is the search box itself",
              'id="merchant-search"' in html
              and html.index('id="merchant-search"') < html.index('name="new_merchant_name"') + 200)
        check("an empty option exists so a chip can be cleared without JavaScript",
              'id="m-none"' in html)
        check("the merchant block is marked so a transfer can hide it",
              "data-merchant-block" in html)

        r = post_entry(c, amount="12", merchant_id="", account_id="1")
        check("clearing the chip saves an entry with no merchant",
              r.status_code == 302
              and sql("SELECT merchant_id FROM transactions ORDER BY id DESC LIMIT 1")
                  ["merchant_id"] is None)

        r = post_entry(c, amount="300", direction="transfer", account_id="1",
                       counter_account_id="3", merchant_id="60")
        check("a transfer drops a merchant even if one is posted",
              r.status_code == 302
              and sql("SELECT merchant_id FROM transactions ORDER BY id DESC LIMIT 1")
                  ["merchant_id"] is None)

    # ---------------------------------------------------------- logout
    print("\nsigning out asks first")
    with app.test_client() as c:
        login(c)
        r = c.get("/logout")
        check("the sign out link opens a question, not an exit", r.status_code == 200)
        check("with the deed behind a POST", b'action="/logout"' in r.data and b"method=\"post\"" in r.data.lower())
        check("and a way back", b"Stay signed in" in r.data)
        check("looking at the question does not end the session", c.get("/").status_code == 200)

        r = c.post("/logout", data={"_csrf": token(c.get("/logout").data)})
        check("confirming signs out", r.status_code == 302)
        check("and the form is behind the login again",
              c.get("/").headers.get("Location", "").startswith("/login"))

    # ---------------------------------------------------- periodic limits
    print("\nthe cash withdrawal ceiling is a day, not a single withdrawal")
    with app.test_client() as c:
        login(c)
        post_account(c, name="Small Debit", type="debit_card", currency="EGP",
                     parent_account_id="1", card_network="Meeza", card_color="navy",
                     withdrawal_limit="1000", card_expires_on="2099-12")
        small = sql("SELECT id FROM accounts WHERE name='Small Debit'")["id"]

        r = post_entry(c, amount="600", account_id=str(small), direction="transfer",
                       counter_account_id="2", occurred_on="2026-08-01")
        check("the first withdrawal of the day goes through", r.status_code == 302)

        r = post_entry(c, amount="600", account_id=str(small), direction="transfer",
                       counter_account_id="2", occurred_on="2026-08-01")
        check("a second one that breaks the day's ceiling is refused",
              r.status_code == 400 and b"daily cash withdrawal limit" in r.data)
        check("and it says how much is left", b"400.00 left today" in r.data)

        r = post_entry(c, amount="600", account_id=str(small), direction="transfer",
                       counter_account_id="2", occurred_on="2026-08-02")
        check("the ceiling resets the next day", r.status_code == 302)

    print("\ncredit limits are a month, and local is not international")
    with app.test_client() as c:
        login(c)
        post_account(c, name="Tight Credit", type="credit_card", currency="EGP",
                     card_network="Visa", card_color="#333333", withdrawal_limit="1000",
                     credit_limit_local="1000", credit_limit_intl="500",
                     card_expires_on="2099-12")
        tight = sql("SELECT id FROM accounts WHERE name='Tight Credit'")["id"]

        r = post_entry(c, amount="600", account_id=str(tight), occurred_on="2026-08-05")
        check("a charge inside the month's local limit is fine", r.status_code == 302)

        r = post_entry(c, amount="600", account_id=str(tight), occurred_on="2026-08-06")
        check("one that breaks it is refused",
              r.status_code == 400 and b"monthly local limit" in r.data)
        check("with what is left of the month", b"400.00 left this month" in r.data)

        r = post_entry(c, amount="600", account_id=str(tight), occurred_on="2026-07-05")
        check("last month has its own allowance", r.status_code == 302)

        r = post_entry(c, amount="5", account_id=str(tight), currency="USD",
                       fx_rate_to_base="50", occurred_on="2026-08-07")
        check("a foreign charge counts against the international limit instead",
              r.status_code == 302)

        r = post_entry(c, amount="6", account_id=str(tight), currency="USD",
                       fx_rate_to_base="50", occurred_on="2026-08-08")
        check("which is a separate, smaller ceiling",
              r.status_code == 400 and b"monthly international limit" in r.data)

    # ----------------------------------------------------------- expiry
    print("\ncards switch themselves off when they expire")
    with app.test_client() as c:
        login(c)
        r = post_account(c, name="No Expiry", type="debit_card", currency="EGP",
                         parent_account_id="1", card_network="Visa", card_color="teal",
                         withdrawal_limit="1000")
        check("a card without an expiry date is refused",
              r.status_code == 400 and b"expiry" in r.data)

        r = post_account(c, name="Odd Expiry", type="debit_card", currency="EGP",
                         parent_account_id="1", card_network="Visa", card_color="teal",
                         withdrawal_limit="1000", card_expires_on="July 2029")
        check("and one with an expiry that is not a month",
              r.status_code == 400 and b"2029-07" in r.data)

        r = post_account(c, name="Old Card", type="debit_card", currency="EGP",
                         parent_account_id="1", card_network="Visa", card_color="teal",
                         withdrawal_limit="1000", card_expires_on="2020-01")
        check("saving an already-expired card switches it off",
              r.status_code == 302
              and sql("SELECT is_active FROM accounts WHERE name='Old Card'")["is_active"] == 0)

        # A card that expires while nobody is looking is the real case.
        conn = dbmod.connect(T.DATABASE_PATH)
        conn.execute("UPDATE accounts SET is_active = 1 WHERE name = 'Old Card'")
        conn.commit()
        conn.close()
        c.get("/settings/accounts")
        check("and reading the accounts list switches off one that expired since",
              sql("SELECT is_active FROM accounts WHERE name='Old Card'")["is_active"] == 0)

        conn = dbmod.connect(T.DATABASE_PATH)
        conn.execute("UPDATE accounts SET is_active = 1 WHERE name = 'Old Card'")
        conn.commit()
        conn.close()
        c.get("/")
        check("so does opening the entry form",
              sql("SELECT is_active FROM accounts WHERE name='Old Card'")["is_active"] == 0)

        check("a card still in date is left alone",
              sql("SELECT is_active FROM accounts WHERE name='Small Debit'")["is_active"] == 1)

    # -------------------------------------------------------- fx rates
    print("\nexchange rates are cached, never fetched during a request")
    conn = dbmod.connect(T.DATABASE_PATH)
    conn.execute(
        "INSERT INTO fx_rates (base, currency, rate_to_base, fetched_at, source) VALUES "
        "('EGP','EUR',58.5,?,'test'), ('EGP','USD',50.25,?,'test')",
        (dbmod.utc_now(), dbmod.utc_now()))
    conn.commit()
    conn.close()

    with app.test_client() as c:
        login(c)
        html = c.get("/").data.decode()
        check("the currency list carries the cached rate", 'data-rate="58.5"' in html)
        check("and how old it is", 'data-rate-age="0"' in html)
        # EGP is the base, so there is nothing to convert and no rate to offer.
        # The option tag spans lines in the template, hence the window rather
        # than a line match.
        at = html.find('<option value="EGP"')
        check("the base currency offers no rate to itself",
              at != -1 and 'data-rate=""' in html[at:at + 200])

    with app.app_context():
        import fx
        check("a fresh cache is not refetched", fx.stale("EGP", 7) is False)
        check("a week-old one is", fx.stale("EGP", 0) is True)
        check("and a base nobody has fetched is", fx.stale("JPY", 7) is True)
        check("rates read back as numbers, not text",
              isinstance(fx.cached("EGP")["EUR"]["rate"], float))
        # The provider quotes "1 EGP = 0.0172 EUR"; a transaction stores the
        # other direction, and getting this backwards is a silent 3000x error.
        check("the stored direction is foreign → base",
              fx.cached("EGP")["EUR"]["rate"] > 1)

        # The check above reads back a row this file wrote, so it proves the
        # cache round-trips — not that fetch() inverts. The inversion is the
        # only place the 3000x error can actually be introduced, so it gets its
        # own check with the provider stubbed out. Nothing here touches the
        # network; urlopen is replaced for the duration.
        import io
        import json as _json
        import urllib.request

        class _FakeResponse(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                self.close()
                return False

        # SAR is quoted above 1 EGP-per-unit, EUR below — both directions in
        # one payload. Only currencies in fx.WANTED come back.
        quoted = {"rates": {"EUR": 0.0172, "USD": 0.0199, "SAR": 0.0747,
                            "JPY": 3.05, "EGP": 1.0}}
        real_urlopen = urllib.request.urlopen
        urllib.request.urlopen = lambda *a, **k: _FakeResponse(
            _json.dumps(quoted).encode())
        try:
            fetched = fx.fetch("EGP")
        finally:
            urllib.request.urlopen = real_urlopen

        check("fetch inverts the provider's quote", abs(fetched["EUR"] - 1 / 0.0172) < 1e-9)
        check("…so one EUR is worth tens of EGP, not thousandths",
              50 < fetched["EUR"] < 70)
        check("…and every wanted currency comes back inverted",
              abs(fetched["SAR"] - 1 / 0.0747) < 1e-9)
        check("a currency the app does not use is dropped", "JPY" not in fetched)
        check("the base is never stored against itself", "EGP" not in fetched)

    # --------------------------------------------------- one clock, everywhere
    print("\nevery screen reads the same calendar")
    # Invariant 3 says the calendar day is the *reader's*, not the server's.
    # This is a source check rather than a behavioural one because reproducing
    # it behaviourally needs the test host in a different timezone from the
    # fixture user, which is not something a check can arrange for itself. The
    # failure it guards against is quiet: a card reading live on one screen and
    # expired on another for up to a day.
    here = Path(__file__).resolve().parent
    offenders = []
    for path in sorted(here.glob("blueprints/*.py")) + [here / "transactions.py"]:
        if not path.exists():
            continue
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if "date.today()" in line and not line.lstrip().startswith("#"):
                offenders.append(f"{path.name}:{n}")
    check("no request-path code reads the server's local date"
          + (f" — found {', '.join(offenders)}" if offenders else ""),
          not offenders)

    # ------------------------------------------------------------- emoji
    print("\naccounts are scannable at a glance")
    with app.test_client() as c:
        login(c)
        html = c.get("/settings/accounts").data.decode()
        check("a bank carries a bank emoji", "🏦" in html)
        check("a card carries a card emoji", "🏧" in html or "💳" in html)
        check("the currency carries its flag", "🇪🇬" in html)
        check("and the type is still spelled out for anyone without an emoji font",
              "debit card" in html and "EGP" in html)

    print("\nincome sources sit under the search box, not in the till row")
    with app.test_client() as c:
        login(c)
        html = c.get("/").data.decode()
        till_row = html.split('id="more"')[0]
        check("the pinned row is spending only", "Acme Payroll" not in till_row)
        check("income sources are still reachable, further down", "Acme Payroll" in html)
        check("and clearing a selection stays pinned", 'id="m-none"' in till_row)

    # ------------------------------------------------------- every screen
    print("\nevery screen still renders")
    with app.test_client() as c:
        login(c)
        bank = 1
        debit_id = sql("SELECT id FROM accounts WHERE name='CIB Debit'")["id"]
        credit_id = sql("SELECT id FROM accounts WHERE name='Two Limits'")["id"]
        insta_id = sql("SELECT id FROM accounts WHERE instapay_handle='@sam_pay'")["id"]
        pages = {
            "the entry form": "/",
            "the month tab": "/dashboard",
            "setup": "/settings/",
            "the accounts list": "/settings/accounts",
            "a new account": "/settings/accounts/new",
            "a new card, pre-linked from the + button":
                f"/settings/accounts/new?type=debit_card&parent={bank}",
            "a bank account": f"/settings/accounts/{bank}",
            "a debit card": f"/settings/accounts/{debit_id}",
            "a credit card": f"/settings/accounts/{credit_id}",
            "an instapay handle": f"/settings/accounts/{insta_id}",
            "categories": "/settings/categories",
            "a category": "/settings/categories/1",
            "merchants": "/settings/merchants",
            "a merchant": "/settings/merchants/60",
            "an income source": "/settings/merchants/61",
        }
        for label, url in pages.items():
            check(f"{label} renders", c.get(url).status_code == 200)

        html = c.get("/settings/accounts").data.decode()
        check("the accounts list offers to add a card to a bank", "type=debit_card" in html)
        check("and offers instapay only where there is not one already",
              html.count("type=instapay") == 1)  # only Aman Wallet is still free
        check("a card's colour is painted as an SVG fill, not an inline style",
              'fill="#1F6F63"' in html and "style=" not in html)

    # ------------------------------------------------------------- css
    print("\nsettings rows respond to the pointer")
    css = (Path(__file__).parent / "static" / "css" / "app.css").read_text(encoding="utf-8")
    check("a hover state exists for settings rows", ".list__item:hover" in css)
    check("it is behind a hover-capable query, so phones do not stick",
          "@media (hover: hover)" in css)
    check("touch gets the same feedback through :active", ".list__item:active" in css)
    check("and the keyboard gets a focus ring", ".list__item:focus-visible" in css)

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
