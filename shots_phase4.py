"""Screenshot the Phase 4 screens at 380px, light and dark.

Its own seed again, for one reason: the month comparison needs *two* months of
history on one account, and every other shots script seeds a single month. A
comparison card photographed against an empty July says "nothing to compare
yet", which is a real state and not the one worth looking at.

Dates are computed from today rather than hard-coded, so the card always has a
this-month and a last-month whenever this is run — a fixed 2026-08 seed produces
an empty comparison the moment the calendar moves on.
"""

import os
import subprocess
import sys
import tempfile
import time
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import db as dbmod
from config import Config
from migrate import migrate
from playwright.sync_api import sync_playwright
from werkzeug.security import generate_password_hash

PORT = 5077
BASE = f"http://127.0.0.1:{PORT}"
OUT = Path("shots-phase4")
OUT.mkdir(exist_ok=True)

tmpdir = Path(tempfile.mkdtemp())
db_path = tmpdir / "shots.db"
uploads = tmpdir / "uploads"
uploads.mkdir()

today = date.today()
first = today.replace(day=1)
last_month = (first.replace(year=first.year - 1, month=12) if first.month == 1
              else first.replace(month=first.month - 1))


def this(day: int) -> str:
    # Never past today: a spend dated later this month is legal but reads as a
    # mistake in a screenshot.
    return first.replace(day=min(day, today.day)).isoformat()


def prev(day: int) -> str:
    return last_month.replace(day=day).isoformat()


conn = dbmod.connect(db_path)
migrate(conn, Config.MIGRATIONS_DIR, log=lambda *_: None)
pw = generate_password_hash("shots-password")
conn.execute(
    "INSERT INTO users (id, username, display_name, password_hash, role, default_shared, "
    "timezone, is_active, created_at) VALUES "
    "(1,'sam','Sam',?,'admin',1,'Africa/Cairo',1,'t'),"
    "(2,'lea','Lea',?,'member',1,'Africa/Cairo',1,'t')", (pw, pw))
# The reporting flag on the current account and off the cash pocket, which is
# the distinction the tick box exists to let a household make.
conn.execute(
    "INSERT INTO accounts (id, name, type, currency, opening_balance_minor, is_active, "
    "sort_order, reporting_enabled, created_at) VALUES "
    "(2,'CIB Current','bank','EGP',850000,1,20,1,'t'),"
    "(3,'Cash','cash','EGP',40000,1,30,0,'t'),"
    "(5,'N26','bank','EUR',120000,1,50,0,'t')")
conn.execute(
    "INSERT INTO account_owners (account_id, user_id, created_at) VALUES "
    "(2,1,'t'), (2,2,'t'), (3,1,'t'), (5,1,'t')")
conn.execute(
    "INSERT INTO merchants (id, name, default_category_id, default_is_online, "
    "default_account_id, is_system, is_active, created_at) VALUES "
    "(50,'Seoudi',1,0,2,0,1,'t'), (51,'Gad',102,0,2,0,1,'t'), (52,'Uber',112,1,2,0,1,'t'),"
    "(55,'Talabat',103,1,2,0,1,'t')")


def spend(when, amount, merchant, category, user=1, shared=1):
    conn.execute(
        "INSERT INTO transactions (user_id, occurred_on, direction, amount_minor, currency, "
        "account_id, merchant_id, category_id, is_online, is_shared, receiptless, "
        "created_at, updated_at) VALUES (?,?,'spend',?,'EGP',2,?,?,0,?,0,'t','t')",
        (user, when, amount, merchant, category, shared))


# Last month: groceries heavy, a lot of eating out, some transport.
for when, amount, merchant, category in [
        (prev(3), 121000, 50, 1), (prev(9), 48250, 50, 1), (prev(17), 62400, 51, 102),
        (prev(21), 27400, 55, 103), (prev(24), 31900, 55, 103), (prev(27), 13500, 52, 112)]:
    spend(when, amount, merchant, category)

# This month: groceries down, eating out nearly stopped, transport up — so the
# card has a fall, a rise and a "stopped" badge in it rather than one arrow
# repeated six times.
for when, amount, merchant, category in [
        (this(2), 96000, 50, 1), (this(6), 34200, 50, 1), (this(11), 41800, 52, 112),
        (this(13), 22600, 52, 112), (this(15), 9900, 51, 102)]:
    spend(when, amount, merchant, category)

conn.execute(
    "INSERT INTO transactions (user_id, occurred_on, direction, amount_minor, currency, "
    "account_id, category_id, is_online, is_shared, receiptless, created_at, updated_at) "
    "VALUES (1,?,'income',1250000,'EGP',2,NULL,0,1,0,'t','t')", (this(4),))
conn.execute(
    "INSERT INTO transactions (user_id, occurred_on, direction, amount_minor, currency, "
    "account_id, category_id, is_online, is_shared, receiptless, created_at, updated_at) "
    "VALUES (1,?,'income',1250000,'EGP',2,NULL,0,1,0,'t','t')", (prev(4),))
# A transfer, so the entries list is photographed with all three colours in it.
conn.execute(
    "INSERT INTO transactions (user_id, occurred_on, direction, amount_minor, currency, "
    "account_id, counter_account_id, counter_amount_minor, counter_currency, is_online, "
    "is_shared, receiptless, created_at, updated_at) VALUES "
    "(1,?,'transfer',200000,'EGP',2,3,200000,'EGP',0,1,0,'t','t')", (this(12),))
conn.commit()
conn.close()

env = {**os.environ, "FLASK_APP": "app", "DATABASE_PATH": str(db_path),
       "UPLOAD_DIR": str(uploads), "SECRET_KEY": "shots-key",
       "SESSION_COOKIE_SECURE": "0"}
server = subprocess.Popen([sys.executable, "-m", "flask", "run", "--port", str(PORT)],
                          env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
time.sleep(3)

try:
    with sync_playwright() as p:
        browser = p.chromium.launch()
        for scheme in ("light", "dark"):
            ctx = browser.new_context(viewport={"width": 380, "height": 780},
                                      device_scale_factor=2, color_scheme=scheme)
            page = ctx.new_page()

            # The offline page first, while signed out — which is the state the
            # service worker caches it in, and the one worth checking looks
            # right rather than like a page missing its layout.
            page.goto(f"{BASE}/offline")
            page.wait_for_timeout(300)
            page.screenshot(path=OUT / f"offline-{scheme}.png", full_page=True)

            page.goto(f"{BASE}/login")
            page.fill("#username", "sam")
            page.fill("#password", "shots-password")
            page.click("button[type=submit]")
            page.wait_for_load_state("networkidle")

            # The comparison card: this month against last, category by
            # category, on the account that asked to be watched.
            page.goto(f"{BASE}/accounts/2")
            page.wait_for_load_state("networkidle")
            page.wait_for_timeout(400)
            page.screenshot(path=OUT / f"account-compare-{scheme}.png", full_page=True)

            # The same screen on an account that did not ask: no card, which is
            # the half of the feature that is easy to forget to look at.
            page.goto(f"{BASE}/accounts/3")
            page.wait_for_load_state("networkidle")
            page.wait_for_timeout(400)
            page.screenshot(path=OUT / f"account-no-compare-{scheme}.png", full_page=True)

            # The tick box that turns it on.
            page.goto(f"{BASE}/settings/accounts/2")
            page.wait_for_timeout(400)
            page.screenshot(path=OUT / f"account-form-reporting-{scheme}.png",
                            full_page=True)

            # The list with the download link under the count.
            page.goto(f"{BASE}/transactions")
            page.wait_for_timeout(400)
            page.screenshot(path=OUT / f"entries-export-{scheme}.png")

            # And with filters open, since the link carries whatever is applied.
            page.goto(f"{BASE}/transactions?user_id=all&direction=spend")
            page.wait_for_timeout(400)
            page.screenshot(path=OUT / f"entries-export-filtered-{scheme}.png",
                            full_page=True)

            ctx.close()
        browser.close()
finally:
    server.terminate()
    server.wait(timeout=10)

print("shots written:", sorted(p.name for p in OUT.glob("*.png")))
