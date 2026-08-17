"""Verification for phase 4: the month comparison and the CSV export.

Two features that look unrelated and fail the same way — by producing a number
that is *plausible*. A comparison card is a wall of small figures nobody can
check by eye, and a spreadsheet is a wall of them nobody checks at all, because
a file that opened is a file that worked.

So both are checked against arithmetic done a second way here, not against
themselves. The comparison's per-category rows have to sum to its own totals;
the export's rows have to be the rows the screen would have shown, including the
ones section 4 keeps out — a download is a transaction read like any other and
is not a way around it.

The formula-injection checks are the one thing here that is about a tool this
app does not control. A cell starting `=` is a program to Excel and to
LibreOffice, and `=HYPERLINK("http://…"&A1)` in a merchant name is how a
household's ledger leaves the house. The quote prefix is the standard defence
and it is checked on every column a person can type into, not just the ones that
seemed likely.
"""

from __future__ import annotations

import csv
import io
import re
import sys
import tempfile
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import accounts as acct
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
        DATABASE_PATH = tmpdir / "phase4.db"
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
        "(2,'lea','Lea',?,'member',1,'Africa/Cairo',1,'t')", (pw, pw))
    conn.execute(
        "INSERT INTO accounts (id, name, type, currency, opening_balance_minor, is_active, "
        "sort_order, reporting_enabled, created_at) VALUES "
        "(1,'CIB','bank','EGP',100000,1,10,1,'t'),"
        "(2,'N26','bank','EUR',50000,1,20,0,'t')")
    conn.execute(
        "INSERT INTO accounts (id, name, type, currency, parent_account_id, "
        "opening_balance_minor, is_active, sort_order, created_at, card_network, card_color, "
        "card_expires_on, withdrawal_limit_minor) VALUES "
        "(3,'CIB Debit','debit_card','EGP',1,0,1,30,'t','Visa','#1F6F63','2099-12',600000)")
    # Two parents and a child, so the comparison groups the way the summary does.
    # Named out of the way of whatever 002 seeded, since a root category's name
    # is unique across the table.
    conn.execute(
        "INSERT INTO categories (id, name, icon, is_active, sort_order) VALUES "
        "(901,'Fixture Food','🍲',1,10), (902,'Fixture Transport','🚌',1,20), "
        "(903,'Fixture Groceries',NULL,1,30)")
    conn.execute("UPDATE categories SET parent_id = 901 WHERE id = 903")
    conn.execute(
        "INSERT INTO merchants (id, name, kind, default_is_online, is_system, is_active, "
        "created_at) VALUES (60,'Seoudi','spend',0,0,1,'t'), "
        "(61,'=cmd|''/c calc''!A1','spend',0,0,1,'t')")
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
    AUG = date(2026, 8, 15)

    # ---------------------------------------------------------- migration

    print("\nmigration 007")
    cols = {r["name"]: r for r in sql("PRAGMA table_info(accounts)")}
    check("accounts has a reporting flag", "reporting_enabled" in cols)
    check("off unless asked for", cols["reporting_enabled"]["dflt_value"] in ("0", 0))
    check("and not nullable, so a template never tests None",
          cols["reporting_enabled"]["notnull"] == 1)
    try:
        run("UPDATE accounts SET reporting_enabled = 2 WHERE id = 1")
        check("only 0 or 1 goes in", False)
    except Exception:
        check("only 0 or 1 goes in", True)

    print("\nthe month before this one")
    check("August's previous month is July", acct.previous_month(date(2026, 8, 15))
          == date(2026, 7, 1))
    check("January's is December of the year before",
          acct.previous_month(date(2026, 1, 9)) == date(2025, 12, 1))
    check("the 31st does not fall through a 30-day month",
          acct.previous_month(date(2026, 5, 31)) == date(2026, 4, 1))
    check("the 29th of March does not land on a February that has no 29th",
          acct.previous_month(date(2026, 3, 29)) == date(2026, 2, 1))

    # --------------------------------------------------------- comparison

    print("\nthis month against last, one account")
    # July: 300 food, 100 transport. August: 150 food, nothing on transport,
    # and 80 on a category that did not exist in July.
    txn(occurred_on="2026-07-03", amount_minor=20000, category_id=901)
    txn(occurred_on="2026-07-14", amount_minor=10000, category_id=903)   # child of Food
    txn(occurred_on="2026-07-20", amount_minor=10000, category_id=902)
    txn(occurred_on="2026-08-04", amount_minor=15000, category_id=901)
    txn(occurred_on="2026-08-09", amount_minor=8000, category_id=None)  # Uncategorised
    txn(occurred_on="2026-08-11", direction="income", amount_minor=50000)
    txn(occurred_on="2026-07-11", direction="income", amount_minor=40000)

    with app.app_context(), app.test_request_context():
        vis_sql, vis_params = visibility_sql(admin)
        cmp = acct.month_compare(1, AUG, vis_sql, vis_params)

        check("the months are named, not numbered",
              (cmp["this_label"], cmp["last_label"]) == ("August", "July"))
        check("this month's spend is the month card's spend",
              cmp["spent"]["this"] == 23000)
        check("last month's too", cmp["spent"]["last"] == 40000)
        check("the difference is signed the way it is spoken — spent less, so down",
              cmp["spent"]["delta"] == -17000)
        check("income is compared as well as spending",
              (cmp["received"]["this"], cmp["received"]["last"]) == (50000, 40000))

        rows = {r["name"]: r for r in cmp["rows"]}
        check("a child category is compared under its parent, as everywhere else",
              rows["Fixture Food"]["last"] == 30000)
        check("and this month's Food is Food alone", rows["Fixture Food"]["this"] == 15000)
        check("a category nobody touched this month is still a row",
              rows["Fixture Transport"]["this"] == 0 and rows["Fixture Transport"]["last"] == 10000)
        check("and is called gone rather than down 100%", rows["Fixture Transport"]["is_gone"])
        check("a category that only exists this month is called new",
              rows["Uncategorised"]["is_new"])
        check("with no percentage, because there is nothing to divide by",
              rows["Uncategorised"]["pct"] is None)
        check("Food halved is reported as halved",
              rows["Fixture Food"]["pct"] == -50)
        check("biggest movement first", [r["name"] for r in cmp["rows"]][0] == "Fixture Food")
        check("the rows account for the totals, both months",
              sum(r["this"] for r in cmp["rows"]) == cmp["spent"]["this"]
              and sum(r["last"] for r in cmp["rows"]) == cmp["spent"]["last"])

        # The one place in the app where up is bad. Worth stating as a check so
        # a later refactor that "fixes" the sign has something to fail against.
        check("spending more than last month is a positive delta",
              acct.month_compare(1, date(2026, 7, 15), vis_sql, vis_params)
              ["spent"]["delta"] > 0)

    print("\nthe comparison is a transaction read, so section 4 still applies")
    private = txn(occurred_on="2026-08-12", amount_minor=90000, category_id=901,
                  user_id=1, is_shared=0)
    with app.app_context(), app.test_request_context():
        vis_sql, vis_params = visibility_sql(lea)
        hers = acct.month_compare(1, AUG, vis_sql, vis_params)
        check("Lea's comparison does not contain the admin's private purchase",
              hers["spent"]["this"] == 23000)
        vis_sql, vis_params = visibility_sql(admin)
        his = acct.month_compare(1, AUG, vis_sql, vis_params)
        check("the admin's own does", his["spent"]["this"] == 113000)
        check("nobody signed in gets nothing rather than everything",
              acct.month_compare(1, AUG, *visibility_sql(None))["spent"]["this"] == 0)
    run("DELETE FROM transactions WHERE id = ?", (private,))

    print("\nthe card is opt-in, and only where it means something")
    with app.test_client() as client:
        login(client)
        page = client.get("/accounts/1").data
        check("an account with the flag on gets the card",
              b"August against July" in page)
        check("and names the months rather than saying 'last month'",
              b"August against July" in page and b"Fixture Food" in page)
        page = client.get("/accounts/2").data
        check("an account with it off does not", b"against July" not in page)

        form = client.get("/settings/accounts/new").data
        check("the account form offers the tick", b'name="reporting_enabled"' in form)
        check("hidden on a card, whose figures are its parent's",
              b'data-when-type="bank credit_card cash wallet"' in form)

        client.post("/settings/accounts/new", data={
            "name": "Wallet", "type": "cash", "currency": "EGP",
            "opening_balance": "", "sort_order": "50", "is_active": "1",
            "reporting_enabled": "1", "_csrf": token(form)})
        made = sql("SELECT reporting_enabled FROM accounts WHERE name = 'Wallet'")
        check("ticking it on a new account sticks", made and made[0][0] == 1)

        card = client.get("/settings/accounts/new").data
        client.post("/settings/accounts/new", data={
            "name": "Second card", "type": "debit_card", "currency": "EGP",
            "parent_account_id": "1", "opening_balance": "", "sort_order": "60",
            "is_active": "1", "reporting_enabled": "1", "withdrawal_limit": "5000",
            "card_network": "Visa", "card_color": "#1F6F63",
            "card_expires_on": "2099-12", "_csrf": token(card)})
        forced = sql("SELECT reporting_enabled FROM accounts WHERE name = 'Second card'")
        check("a card cannot be made to report, whatever the form posts",
              forced and forced[0][0] == 0)

    # ------------------------------------------------------------- export

    print("\nthe list, as a file")
    with app.test_client() as client:
        login(client)
        page = client.get("/transactions").data
        check("the list offers the download", b"Download all as CSV" in page)
        check("and the link carries the filters that are applied",
              b"transactions.csv?" in page)

        res = client.get("/transactions.csv")
        check("the file comes back as a file, not a page",
              res.headers["Content-Type"].startswith("text/csv"))
        check("named so a phone knows what it saved",
              "attachment" in res.headers.get("Content-Disposition", "")
              and ".csv" in res.headers.get("Content-Disposition", ""))
        check("and not cached, since it is somebody's ledger",
              "no-store" in res.headers.get("Cache-Control", ""))
        check("with the byte order mark Excel wants for its Arabic and its €",
              res.data.startswith(b"\xef\xbb\xbf"))

        body = res.data.decode("utf-8-sig")
        rows = list(csv.reader(io.StringIO(body)))
        header, data = rows[0], rows[1:]
        check("a header row a person can read", header[0] == "date" and "amount" in header)
        check("every entry visible on screen is in it",
              len(data) == len(sql("SELECT id FROM transactions")))
        check("oldest first, because a spreadsheet is read downwards",
              data[0][0] <= data[-1][0])

        amount = header.index("amount")
        check("amounts are written out, not rounded into a float",
              data[0][amount] == "200.00")
        check("and the currency travels next to the number",
              data[0][header.index("currency")] == "EGP")

        # Fifty is what the screen shows; the file is not the screen.
        for n in range(60):
            txn(occurred_on="2026-08-01", amount_minor=100 + n)
        res = client.get("/transactions.csv")
        after = list(csv.reader(io.StringIO(res.data.decode("utf-8-sig"))))
        check("the file is not capped at the fifty the screen shows",
              len(after) - 1 > 50)
        run("DELETE FROM transactions WHERE amount_minor < 10000")

    print("\nthe file is filtered the same way the screen is")
    secret = txn(occurred_on="2026-08-13", amount_minor=77700, user_id=1, is_shared=0)
    with app.test_client() as client:
        login(client, "lea")
        # user_id=all, or the person filter defaults to Lea and the file is
        # empty for a reason that has nothing to do with what is being checked.
        body = client.get("/transactions.csv?user_id=all").data.decode("utf-8-sig")
        check("a private entry is not in somebody else's download",
              "777.00" not in body)
        filtered = client.get(
            "/transactions.csv?user_id=all&direction=income").data.decode("utf-8-sig")
        lines = list(csv.reader(io.StringIO(filtered)))[1:]
        check("a filter on the screen is a filter in the file",
              lines and all(r[1] == "income" for r in lines))
        dated = client.get(
            "/transactions.csv?user_id=all&from=2026-07-01&to=2026-07-31"
        ).data.decode("utf-8-sig")
        lines = list(csv.reader(io.StringIO(dated)))[1:]
        check("and so is a date range",
              lines and all(r[0].startswith("2026-07") for r in lines))

    with app.test_client() as client:
        res = client.get("/transactions.csv")
        check("signed out, there is no file at all", res.status_code in (302, 401))
    run("DELETE FROM transactions WHERE id = ?", (secret,))

    print("\na cell is not a program")
    injected = txn(occurred_on="2026-08-14", merchant_id=61, note="=1+1",
                   amount_minor=1500, category_id=901)
    with app.test_client() as client:
        login(client)
        body = client.get("/transactions.csv").data.decode("utf-8-sig")
        rows = [r for r in csv.reader(io.StringIO(body)) if r and r[0] == "2026-08-14"]
        check("the row is there", len(rows) == 1)
        row = rows[0]
        header = list(csv.reader(io.StringIO(body)))[0]
        check("a merchant name that is a formula is quoted out of being one",
              row[header.index("merchant")].startswith("'="))
        check("and so is a note", row[header.index("note")].startswith("'="))
        check("the text itself is still readable underneath",
              "calc" in row[header.index("merchant")])
        check("an ordinary cell is left alone",
              not row[header.index("account")].startswith("'"))
    run("DELETE FROM transactions WHERE id = ?", (injected,))

    # ---------------------------------------------------------------- PWA

    print("\ninstallable on a phone")
    import json

    with app.test_client() as client:
        res = client.get("/manifest.webmanifest")
        check("the manifest is served from the root",
              res.status_code == 200)
        check("as a manifest, not as whatever mimetypes guessed",
              res.headers["Content-Type"].startswith("application/manifest+json"))
        m = json.loads(res.data)
        check("it opens at the app, not at a deep link", m["start_url"] == "/")
        check("it owns the whole app, so no link escapes to a browser tab",
              m["scope"] == "/")
        check("standalone, which is what removes the address bar",
              m["display"] == "standalone")
        check("with the 192 and 512 Android asks for",
              {i["sizes"] for i in m["icons"]} >= {"192x192", "512x512"})
        check("and a maskable one, or Android draws a white square behind it",
              any(i.get("purpose") == "maskable" for i in m["icons"]))
        for icon in m["icons"]:
            got = client.get(icon["src"])
            check(f"{icon['src'].split('/')[-1]} is actually there",
                  got.status_code == 200 and got.data.startswith(b"\x89PNG"))
        # A shortcut to a URL that 404s is a control that lies about what it
        # does, which is the same rule as anywhere else in the app.
        for short in m.get("shortcuts", []):
            # 302 is the sign-in redirect, which is the right answer for a
            # shortcut tapped on a phone whose session has expired. A 308 is
            # not: it means the URL in the manifest is missing a slash and
            # every tap pays a round trip before anything is drawn.
            check(f"the {short['name']} shortcut goes somewhere real",
                  client.get(short["url"]).status_code in (200, 302))

        res = client.get("/sw.js")
        check("the worker is served from the root, or it controls nothing",
              res.status_code == 200)
        check("as JavaScript", "javascript" in res.headers["Content-Type"])
        check("and not cached, or a bad worker outlives its own fix",
              "no-cache" in res.headers.get("Cache-Control", ""))

        res = client.get("/offline")
        check("the offline page needs no session — a redirect to a login page "
              "that is also unreachable helps nobody", res.status_code == 200)
        check("and carries no signed-in name, since a worker freezes it for weeks",
              b"topbar__who" not in res.data)
        check("it says which end is unreachable rather than 'no internet'",
              b"Can't reach home" in res.data)

        page = client.get("/login").data
        check("every page points at the manifest", b'rel="manifest"' in page)
        check("and at an icon iOS will use, which ignores the manifest",
              b'rel="apple-touch-icon"' in page)
        check("and asks iOS for the standalone chrome",
              b'name="apple-mobile-web-app-capable"' in page)

    sw = Path("static/js/sw.js").read_text(encoding="utf-8")
    check("the worker precaches the offline page", '"/offline"' in sw)
    check("and does not cache the ledger, where a stale figure looks live",
          "/transactions" not in sw and '"/"' not in sw)
    check("it only intercepts navigations, so a failed image is not sent HTML",
          'request.mode !== "navigate"' in sw)
    check("app.js registers it at the root scope",
          '"/sw.js"' in Path("static/js/app.js").read_text(encoding="utf-8"))
    for name, size in (("icon-192.png", 192), ("icon-512.png", 512),
                       ("icon-maskable-512.png", 512), ("apple-touch-icon.png", 180)):
        from PIL import Image
        img = Image.open(Path("static/img") / name)
        check(f"{name} is {size}px square", img.size == (size, size))
    check("the apple icon has no transparency, which iOS renders as black",
          Image.open("static/img/apple-touch-icon.png").mode == "RGB")

    # --------------------------------------------------------- deployment

    print("\nthe deployment says what it does")
    gunicorn = Path("deploy/gunicorn.conf.py").read_text(encoding="utf-8")
    check("gunicorn binds loopback, so the only way in is through tailscale",
          '"127.0.0.1:8000"' in gunicorn)
    check("and does not preload, which would fork a shared sqlite handle",
          "preload_app = False" in gunicorn)
    unit = Path("deploy/expenses.service").read_text(encoding="utf-8")
    check("the service migrates before it serves, so a pull is one restart",
          "ExecStartPre" in unit and "app migrate" in unit)
    check("and can write the folder its database is in",
          "ReadWritePaths=" in unit)
    cron = Path("deploy/crontab.example").read_text(encoding="utf-8")
    tasks = Path("deploy/windows-tasks.ps1").read_text(encoding="utf-8")
    for job in ("fetch-rates", "check-limits", "sweep-uploads"):
        check(f"{job} is scheduled on the Pi", job in cron)
        check(f"and on the laptop, which has no cron", job in tasks)
    check("the backup is scheduled too, on both",
          "backup.py" in cron and "backup.py" in tasks)
    deploy_doc = Path("DEPLOY.md").read_text(encoding="utf-8")
    check("the runbook says to use serve rather than funnel",
          "tailscale serve" in deploy_doc and "funnel" in deploy_doc)
    check("and warns about the cookie flag that makes login fail in silence",
          "SESSION_COOKIE_SECURE" in deploy_doc)
    # Two servers, two opposite conventions for naming a factory, and a command
    # that is wrong in a document is wrong at the moment somebody is standing in
    # front of a terminal following it.
    check("the waitress line passes --call, since create_app is a factory",
          "waitress --listen=127.0.0.1:8000 --threads=6 --call app:create_app" in deploy_doc)
    check("and the gunicorn one keeps its parentheses, which is the other one's rule",
          '"app:create_app()"' in unit)

    # ------------------------------------------------------------- source

    print("\nthings that only a reading catches")
    ledger_src = Path("blueprints/ledger.py").read_text(encoding="utf-8")
    check("the export builds its WHERE with the same helper the screen uses",
          ledger_src.count("_where(g.user, f)") >= 2)
    check("no amount is written as a float", "float(" not in ledger_src)
    for lead in ("=", "+", "-", "@"):
        check(f"{lead!r} is treated as dangerous", f'"{lead}"' in ledger_src)
    css = Path("static/css/app.css").read_text(encoding="utf-8")
    check("the delta colours are defined for both directions",
          ".delta--worse" in css and ".delta--better" in css)

    print()
    if failures:
        print(f"{len(failures)} failed:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
