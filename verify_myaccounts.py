"""Verification for account ownership, the account summary, and balance history.

The balance history is the part worth being paranoid about. It is a sequence of
numbers that all look plausible individually, and there is exactly one way to
know it is right: every row's balance, minus that row's own effect, has to be
the next row's balance — all the way down to the opening balance the account was
created with. A single mis-signed leg produces a column of numbers nobody can
tell is wrong by looking.

The second thing checked hard here is that ownership did not accidentally become
visibility. Putting two people on an account must not show either of them a
purchase the other made privately. Section 4 keys off the transaction's owner and
006 did not change that; these checks are what say so out loud.
"""

from __future__ import annotations

import re
import sys
import tempfile
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import accounts as acct
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
        DATABASE_PATH = tmpdir / "acct.db"
        UPLOAD_DIR = tmpdir / "uploads"
        SECRET_KEY = "test-key"
        SESSION_COOKIE_SECURE = False

    conn = dbmod.connect(T.DATABASE_PATH)
    migrate(conn, Config.MIGRATIONS_DIR, log=lambda *_: None)
    pw = generate_password_hash("pw12345678")
    conn.execute(
        "INSERT INTO users (id, username, display_name, password_hash, role, default_shared, "
        "timezone, is_active, created_at) VALUES "
        "(1,'admin','Admin',?,'admin',1,'Africa/Cairo',1,'t'),"
        "(2,'lea','Lea',?,'member',1,'Africa/Cairo',1,'t'),"
        "(3,'sam','Sam',?,'member',1,'Africa/Cairo',1,'t')", (pw, pw, pw))
    # CIB opens at 1,000.00 EGP and is joint. N26 is Lea's alone. Cash is
    # nobody's, which since 006 means the household's.
    conn.execute(
        "INSERT INTO accounts (id, name, type, currency, opening_balance_minor, is_active, "
        "sort_order, created_at) VALUES "
        "(1,'CIB','bank','EGP',100000,1,10,'t'),"
        "(2,'N26','bank','EUR',50000,1,20,'t'),"
        "(4,'Cash','cash','EGP',5000,1,40,'t')")
    conn.execute(
        "INSERT INTO accounts (id, name, type, currency, parent_account_id, "
        "opening_balance_minor, is_active, sort_order, created_at, card_network, card_color, "
        "card_expires_on, withdrawal_limit_minor) VALUES "
        "(3,'CIB Debit','debit_card','EGP',1,0,1,30,'t','Visa','#1F6F63','2099-12',600000)")
    conn.execute(
        "INSERT INTO account_owners (account_id, user_id, created_at) VALUES "
        "(1,1,'t'), (1,2,'t'), (2,2,'t')")
    conn.execute(
        "INSERT INTO merchants (id, name, kind, default_is_online, is_system, is_active, "
        "created_at) VALUES (60,'Seoudi','spend',0,0,1,'t')")
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

    def login(client, username="admin"):
        page = client.get("/login").data
        client.post("/login", data={"username": username, "password": "pw12345678",
                                    "_csrf": token(page)})

    admin = {"id": 1, "role": "admin", "timezone": "Africa/Cairo", "default_shared": 1}
    lea = {"id": 2, "role": "member", "timezone": "Africa/Cairo", "default_shared": 1}
    sam = {"id": 3, "role": "member", "timezone": "Africa/Cairo", "default_shared": 1}
    AUG = date(2026, 8, 15)

    # ----------------------------------------------------------- migration

    print("\nmigration 006")
    names = {r["name"] for r in sql("SELECT name FROM sqlite_master")}
    check("account_owners exists", "account_owners" in names)
    check("and owner_id is retired rather than left as a second answer",
          "trg_accounts_owner_id_retired_insert" in names)
    try:
        run("UPDATE accounts SET owner_id = 1 WHERE id = 1")
        check("writing the old column is refused", False)
    except Exception as exc:
        check("writing the old column is refused", "account_owners" in str(exc))

    # ---------------------------------------------------------- ownership

    print("\nan account can belong to more than one person")
    with app.app_context(), app.test_request_context():
        check("both names come back", {u["display_name"] for u in acct.owners_of(1)}
              == {"Admin", "Lea"})
        check("an account nobody claimed has no names", acct.owners_of(4) == [])

        mine = {a["id"] for a in acct.visible_accounts(lea)}
        check("Lea sees the joint account she is on", 1 in mine)
        check("and the one that is only hers", 2 in mine)
        check("and the card hanging off the joint account, which she can spend from",
              3 in mine)
        check("and the household's cash, which nobody has claimed", 4 in mine)

        theirs = {a["id"] for a in acct.visible_accounts(sam)}
        check("Sam, on nothing, does not see the joint account", 1 not in theirs)
        check("nor its card", 3 not in theirs)
        check("but does see the unclaimed household cash", 4 in theirs)

        check("admin sees every account", len(acct.visible_accounts(admin)) == 4)
        check("nobody signed in sees none", acct.visible_accounts(None) == [])

        acct.set_owners(4, [3])
        check("adding someone to an account puts it in their list",
              4 in {a["id"] for a in acct.visible_accounts(sam)})
        check("and takes it out of everyone else's, since it is claimed now",
              4 not in {a["id"] for a in acct.visible_accounts(lea)})
        acct.set_owners(4, [])
        check("clearing the names hands it back to the household",
              4 in {a["id"] for a in acct.visible_accounts(lea)})

    print("\nowning an account is not seeing what was bought with it")
    private = txn(user_id=1, is_shared=0, amount_minor=9999, account_id=1)
    with app.app_context(), app.test_request_context():
        vis_sql, vis_params = visibility_sql(lea)
        month = acct.month_here(1, AUG, vis_sql, vis_params)
        check("Lea is on the joint account but does not see admin's private spend there",
              month["spent"] == 0)
        vis_sql, vis_params = visibility_sql(admin)
        check("admin does", acct.month_here(1, AUG, vis_sql, vis_params)["spent"] == 9999)
    run("DELETE FROM transactions WHERE id = ?", (private,))

    # ------------------------------------------------------ the arithmetic

    print("\nbalance history walks back to exactly where the account started")
    ids = []
    ids.append(txn(occurred_on="2026-08-01", direction="spend", amount_minor=1000, account_id=1))
    ids.append(txn(occurred_on="2026-08-02", direction="income", amount_minor=2500, account_id=1))
    # On the card, which settles to CIB — this is the leg most easily lost.
    ids.append(txn(occurred_on="2026-08-03", direction="spend", amount_minor=700, account_id=3))
    ids.append(txn(occurred_on="2026-08-04", direction="transfer", amount_minor=1500,
                   account_id=1, counter_account_id=4, counter_amount_minor=1500,
                   counter_currency="EGP"))

    with app.app_context(), app.test_request_context():
        account = sql("SELECT * FROM accounts WHERE id = 1")[0]
        vis_sql, vis_params = visibility_sql(admin)
        h = acct.history(account, admin, vis_sql, vis_params, limit=50)

        expected = 100000 - 1000 + 2500 - 700 - 1500
        check("the headline balance is the one the accounts screen shows",
              h["balance_now"] == bal.account_balances()[1].minor)
        check("and it is what the entries add up to", h["balance_now"] == expected)
        check("every entry that touched the account is listed", h["total"] == 4)
        check("newest first", h["entries"][0]["row"]["occurred_on"] == "2026-08-04")
        check("the top row's balance is the balance now",
              h["entries"][0]["balance_after"] == h["balance_now"])

        # The whole point of the screen: each step has to be the entry's own
        # effect and nothing else.
        steps_ok = all(
            h["entries"][i]["balance_after"] - h["entries"][i]["delta"]
            == h["entries"][i + 1]["balance_after"]
            for i in range(len(h["entries"]) - 1)
        )
        check("each row minus its own effect is the row below it", steps_ok)

        oldest = h["entries"][-1]
        check("and the last step lands on the opening balance",
              oldest["balance_after"] - oldest["delta"] == 100000)

        card_leg = [e for e in h["entries"] if e["row"]["account_id"] == 3][0]
        check("money spent on the card comes out of the account behind it",
              card_leg["delta"] == -700)
        transfer = [e for e in h["entries"] if e["row"]["direction"] == "transfer"][0]
        check("a transfer out is a reduction, not an increase", transfer["delta"] == -1500)

        cash = sql("SELECT * FROM accounts WHERE id = 4")[0]
        into = acct.history(cash, admin, vis_sql, vis_params, limit=50)
        check("and the same transfer is an increase at the other end",
              into["entries"][0]["delta"] == 1500)
        check("cash lands where it should", into["balance_now"] == 5000 + 1500)

    print("\na transfer between a card and the account behind it nets to nothing")
    same = txn(occurred_on="2026-08-05", direction="transfer", amount_minor=2000,
               account_id=3, counter_account_id=1, counter_amount_minor=2000,
               counter_currency="EGP")
    with app.app_context(), app.test_request_context():
        account = sql("SELECT * FROM accounts WHERE id = 1")[0]
        vis_sql, vis_params = visibility_sql(admin)
        h = acct.history(account, admin, vis_sql, vis_params, limit=50)
        both = [e for e in h["entries"] if e["row"]["id"] == same][0]
        check("both legs are counted, so it moves the balance by zero", both["delta"] == 0)
        check("and the balance is unchanged by it",
              h["balance_now"] == bal.account_balances()[1].minor)
    run("DELETE FROM transactions WHERE id = ?", (same,))

    print("\nentries you cannot see still move the balance, and it says so")
    hidden = txn(occurred_on="2026-08-06", direction="spend", amount_minor=4321,
                 account_id=1, user_id=1, is_shared=0)
    with app.app_context(), app.test_request_context():
        account = sql("SELECT * FROM accounts WHERE id = 1")[0]
        vis_sql, vis_params = visibility_sql(lea)
        h = acct.history(account, lea, vis_sql, vis_params, limit=50)

        check("the hidden entry is not in the list",
              hidden not in {e["row"]["id"] for e in h["entries"]})
        check("but the balance still counts it — a filtered balance is a wrong "
              "number, not a partial one",
              h["balance_now"] == bal.account_balances()[1].minor)
        check("and the step it caused is explained rather than left as a mystery",
              any(e["hidden_before"] for e in h["entries"]) or h["hidden_after"])

        vis_admin, params_admin = visibility_sql(admin)
        full = acct.history(account, admin, vis_admin, params_admin, limit=50)
        check("admin and a member see the same balance on the same account",
              full["balance_now"] == h["balance_now"])
        check("they just see a different number of rows",
              full["total"] > h["total"])
    run("DELETE FROM transactions WHERE id = ?", (hidden,))

    print("\nan unconvertible leg is admitted to rather than guessed at")
    odd = txn(occurred_on="2026-08-07", direction="spend", amount_minor=500,
              currency="USD", fx_rate_to_base=None, account_id=1)
    with app.app_context(), app.test_request_context():
        account = sql("SELECT * FROM accounts WHERE id = 1")[0]
        vis_sql, vis_params = visibility_sql(admin)
        h = acct.history(account, admin, vis_sql, vis_params, limit=50)
        leg = [e for e in h["entries"] if e["row"]["id"] == odd][0]
        check("it moves the balance by nothing rather than by a made-up number",
              leg["delta"] == 0)
        check("and is marked as not converted", not leg["exact"])
        check("the whole history says it is approximate", h["approximate"])
    run("DELETE FROM transactions WHERE id = ?", (odd,))

    print("\nthe window, and asking for more of it")
    for i in range(30):
        txn(occurred_on="2026-07-%02d" % (i + 1), amount_minor=100 + i, account_id=1)
    with app.app_context(), app.test_request_context():
        account = sql("SELECT * FROM accounts WHERE id = 1")[0]
        vis_sql, vis_params = visibility_sql(admin)
        first = acct.history(account, admin, vis_sql, vis_params, limit=20)
        check("twenty arrive first", len(first["entries"]) == 20)
        check("and it says how many are behind them", first["more"] == first["total"] - 20)

        more = acct.history(account, admin, vis_sql, vis_params, limit=40)
        check("asking for more gets more", len(more["entries"]) == more["total"])
        check("without changing what the first ones said",
              [e["balance_after"] for e in more["entries"][:20]]
              == [e["balance_after"] for e in first["entries"]])
        check("the walk still ends on the opening balance",
              more["entries"][-1]["balance_after"] - more["entries"][-1]["delta"] == 100000)

    print("\nthe balance shows its working")
    # The exact shape that produced the complaint: an opening balance below
    # zero, income in a foreign currency, a transfer out, and one spend — three
    # numbers on screen that a reader could not reconcile because the two lines
    # closing the gap were not on the screen at all.
    with app.app_context(), app.test_request_context():
        account = sql("SELECT * FROM accounts WHERE id = 1")[0]
        r = acct.reconcile(account)

        check("the column ends on the balance, not near it",
              r["total"] == bal.account_balances()[1].minor)
        check("and it knows that it does", r["agrees"])
        check("the opening balance is a line rather than an assumption",
              r["opening"] == 100000)
        check("income is its own line", r["income"] > 0)
        check("spending is its own line", r["spend"] > 0)
        check("a transfer out is neither of those and gets its own line",
              r["transfer_out"] > 0)
        check("as does a transfer in, at the other end",
              acct.reconcile(sql("SELECT * FROM accounts WHERE id = 4")[0])["transfer_in"] > 0)

        # Arithmetic, not a stored total: the lines have to *be* the sum.
        check("the lines are the sum, so nothing can be added without showing",
              r["opening"] + r["income"] - r["spend"]
              - r["transfer_out"] + r["transfer_in"] == r["total"])

        # A card settles to its parent, so both must reconcile identically —
        # the card has no money of its own to reconcile.
        check("a card reconciles to the account behind it",
              acct.reconcile(sql("SELECT * FROM accounts WHERE id = 3")[0])["total"]
              == r["total"])

    print("\na foreign leg with no rate is left out and said so, never guessed")
    odd = txn(occurred_on="2026-08-08", direction="spend", amount_minor=500,
              currency="USD", fx_rate_to_base=None, account_id=1)
    with app.app_context(), app.test_request_context():
        account = sql("SELECT * FROM accounts WHERE id = 1")[0]
        r = acct.reconcile(account)
        check("it is counted as unconvertible", r["unconverted"] == 1)
        check("and contributes nothing rather than a made-up number",
              r["total"] == bal.account_balances()[1].minor)
    run("DELETE FROM transactions WHERE id = ?", (odd,))

    with app.test_client() as c:
        login(c)
        page = c.get("/accounts/1").data
        check("the summary prints the sum", b"How this balance adds up" in page)
        check("with the opening balance on it", b"Opened with" in page)
        check("and transfers named as movement rather than as spending",
              b"Moved out to other accounts" in page)
        check("the month card says it is only the month, since the sum is not",
              b"This month only" in page)

    print("\nmoney leaving is red")
    css = Path("static/css/app.css").read_text(encoding="utf-8")
    check("spend has a colour of its own", ".amt--spend" in css)
    check("and it is the same red an overdrawn balance uses, not a second one",
          "--over" in css.split(".amt--spend")[1].split("}")[0])
    with app.test_client() as c:
        login(c)
        check("the entry list paints it", b"amt--spend" in c.get("/transactions").data)
        check("so does the balance history",
              b"amt--spend" in c.get("/accounts/1/history").data)

    # --------------------------------------------------------------- wheels

    print("\nlimit wheels")
    with app.app_context(), app.test_request_context():
        card = sql("SELECT * FROM accounts WHERE id = 3")[0]
        run("INSERT INTO limits (name, scope_type, scope_id, period, amount_minor, currency, "
            "warn_pct, is_active, created_at) VALUES "
            "('CIB budget','account',1,'monthly',100000,'EGP',80,1,'t')")
        rings = acct.wheels(card, AUG)
        check("a card shows the bank's daily cash ceiling",
              any("Cash today" in w["label"] for w in rings))
        check("which says it is the bank's and does block a save",
              any("refused" in w["note"] for w in rings))

        bank = sql("SELECT * FROM accounts WHERE id = 1")[0]
        rings = acct.wheels(bank, AUG)
        budget = [w for w in rings if w["label"] == "CIB budget"][0]
        check("a budget on the account shows as a wheel too", budget["ceiling"] == 100000)
        check("and says that it only warns", "only warns" in budget["note"])
        check("the ring is a dash length, not an inline style the CSP would drop",
              " " in budget["dash"] and budget["dash"].replace(".", "").replace(" ", "").isdigit())

        over = acct._wheel("x", 300, 100, "EGP", "n")
        check("past its end the number keeps going", over["pct"] == 300)
        check("but the ring stops at full", over["ring"] == 100)
        check("and it knows it is over", over["over"])

    # -------------------------------------------------------------- screens

    print("\nthe screens")
    with app.test_client() as c:
        login(c, "sam")
        check("Sam's list does not name the joint account",
              b"CIB" not in c.get("/accounts/").data)
        check("and he cannot reach it by typing the URL",
              c.get("/accounts/1").status_code == 404)
        check("404 rather than 403 — which accounts exist is not his business either",
              c.get("/accounts/1").status_code != 403)
        check("nor its history", c.get("/accounts/1/history").status_code == 404)

    with app.test_client() as c:
        login(c, "lea")
        page = c.get("/accounts/").data
        check("Lea's list names the accounts she is on", b"CIB" in page and b"N26" in page)
        check("with the card underneath the account it draws on",
              b"list__item--child" in page)

        page = c.get("/accounts/1").data
        check("the summary opens", b"Balance" in page)
        check("naming both people on the account", b"Admin" in page and b"Lea" in page)
        check("and saying out loud that this is not about who sees what",
              b"who can see the entries" in page or b"follows whoever logged them" in page)
        check("with a way through to the history",
              b"/accounts/1/history" in page)

        page = c.get("/accounts/3").data
        # A short needle: the sentence wraps in the template, so the rendered
        # bytes carry a newline the source string does not.
        check("a card reports the balance of the account behind it",
              b"spends it rather than" in page)

        page = c.get("/accounts/1/history").data
        check("the history draws", b"Balance history" in page)
        check("with a graph of the balance across the window", b"timeline__line" in page)
        check("and a slider to walk along it", b'id="timeline-slider"' in page)
        check("which ships disabled, so it is the script that makes it work",
              b"timeline-slider" in page and b"disabled" in page)
        # Parsed, not grepped. The attribute shipped double-quoted once, which
        # truncated the JSON at the first inner quote — every byte the checks
        # looked for was present and the slider did nothing at all.
        import json
        blob = re.search(rb"data-points='([^']*)'", page)
        check("the points survive being put in an attribute", blob is not None)
        series = json.loads(blob.group(1).decode().replace("\\u0027", "'")) if blob else []
        check("and parse back into one point per row the list shows",
              len(series) == page.count(b"data-entry="))
        check("carrying their own formatted money — money.py owns that, not a "
              "second implementation in JavaScript",
              all("balance_text" in p and "delta_text" in p for p in series))
        check("and coordinates the marker can be moved to",
              all(isinstance(p["x"], (int, float)) for p in series))
        check("and offers the next page as a link, not an infinite scroll",
              b"/accounts/1/history?show=40" in page and b"more" in page)
        check("which works", c.get("/accounts/1/history?show=40").status_code == 200)
        check("a silly show= is clamped rather than walking forever",
              c.get("/accounts/1/history?show=999999").status_code == 200)

    with app.test_client() as c:
        login(c)
        check("admin's list has everything", c.get("/accounts/").data.count(b"list__item") >= 4)
        check("and says why it does", b"every account in the household"
              in c.get("/accounts/").data)

    print("\nthe tab bar")
    with app.test_client() as c:
        login(c, "lea")
        page = c.get("/dashboard").data
        check("Accounts is a tab, not two taps inside Setup", b">Accounts<" in page)
        check("and a member gets it too", b'href="/accounts/"' in page)

    print("\nsetting the owners from the form")
    with app.test_client() as c:
        login(c)
        page = c.get("/settings/accounts/1").data
        check("the form offers tick boxes rather than one 'belongs to'",
              page.count(b'name="owner_ids"') >= 2)
        check("with the current people ticked", b'value="2"' in page and b"checked" in page)
        check("and says ownership is not visibility",
              b"does not hide any purchase" in page)

        c.post("/settings/accounts/1", data={
            "_csrf": token(page), "name": "CIB", "type": "bank", "currency": "EGP",
            "sort_order": "10", "is_active": "1", "owner_ids": ["1", "2", "3"],
        })
        with app.app_context(), app.test_request_context():
            check("saving three names keeps three", acct.owner_ids(1) == {1, 2, 3})

        page = c.get("/settings/accounts/1").data
        c.post("/settings/accounts/1", data={
            "_csrf": token(page), "name": "CIB", "type": "bank", "currency": "EGP",
            "sort_order": "10", "is_active": "1",
        })
        with app.app_context(), app.test_request_context():
            check("and ticking nobody hands it back to the household",
                  acct.owner_ids(1) == set())

        page = c.get("/settings/accounts").data
        check("the accounts list survived losing its owner join", b"CIB" in page)

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
