"""Screenshot the app at 380px, light and dark, for design review.

Runs against a throwaway database seeded with plausible household data, so the
entry form is photographed with real chips rather than an empty state.
"""

import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import db as dbmod
from config import Config
from migrate import migrate
from playwright.sync_api import sync_playwright
from werkzeug.security import generate_password_hash

PORT = 5057
BASE = f"http://127.0.0.1:{PORT}"
OUT = Path("shots")
OUT.mkdir(exist_ok=True)

tmp = Path(tempfile.mkdtemp()) / "shots.db"
conn = dbmod.connect(tmp)
migrate(conn, Config.MIGRATIONS_DIR, log=lambda *_: None)
conn.execute(
    "INSERT INTO users (id, username, display_name, password_hash, role, default_shared, "
    "timezone, is_active, created_at) VALUES (1,'sam','Sam',?,'admin',1,'Africa/Cairo',1,'t')",
    (generate_password_hash("shots-password"),))
conn.execute(
    "INSERT INTO accounts (id, name, type, currency, is_active, sort_order, created_at) "
    "VALUES (2,'CIB Current','bank','EGP',1,20,'t'),"
    "       (3,'Cash','cash','EGP',1,30,'t'),"
    "       (4,'DE Giro','bank','EUR',1,40,'t')")
# An Instapay row needs its handle and the account it draws on (004), and the
# person it belongs to is a row in account_owners now rather than a column (006).
conn.execute(
    "INSERT INTO accounts (id, name, type, currency, parent_account_id, is_active, "
    "sort_order, created_at, instapay_handle) "
    "VALUES (1,'Sam - @sam_pay','instapay','EGP',2,1,10,'t','@sam_pay')")
conn.execute(
    "INSERT INTO account_owners (account_id, user_id, created_at) VALUES (4,1,'t')")
conn.execute(
    "INSERT INTO merchants (id, name, default_category_id, default_is_online, default_account_id, "
    "is_system, is_active, created_at) VALUES "
    "(50,'Seoudi',1,0,1,0,1,'t'), (51,'Gad',102,0,3,0,1,'t'), (52,'Uber',112,1,1,0,1,'t'),"
    "(53,'Carrefour',1,0,2,0,1,'t'), (54,'Amazon',142,1,2,0,1,'t'), (55,'Talabat',103,1,1,0,1,'t')")
for i, mid in enumerate([50, 51, 52, 53, 55]):
    conn.execute(
        "INSERT INTO transactions (user_id, occurred_on, direction, amount_minor, currency, "
        "account_id, merchant_id, category_id, is_online, is_shared, created_at, updated_at) "
        "VALUES (1,'2026-08-1%d','spend',%d,'EGP',1,%d,1,0,1,'2026-08-1%dT10:00:00Z','t')"
        % (i, 4500 + i * 1100, mid, i))
conn.commit()
conn.close()

env = {**os.environ, "FLASK_APP": "app", "DATABASE_PATH": str(tmp),
       "SECRET_KEY": "shots-key", "SESSION_COOKIE_SECURE": "0"}
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

            page.goto(f"{BASE}/login")
            page.fill("#username", "sam")
            page.fill("#password", "shots-password")
            page.click("button[type=submit]")
            page.wait_for_load_state("networkidle")
            page.wait_for_timeout(500)
            page.screenshot(path=OUT / f"entry-{scheme}.png")

            # The screen mid-entry: amount typed, chip tapped.
            page.fill("#amount", "47.50")
            page.click("label[for='m-50']")
            page.wait_for_timeout(300)
            page.screenshot(path=OUT / f"entry-filled-{scheme}.png")

            # After saving: back on a blank form with the confirmation toast.
            page.click("button[type=submit].btn--save")
            page.wait_for_load_state("networkidle")
            page.wait_for_timeout(500)
            page.screenshot(path=OUT / f"entry-saved-{scheme}.png")

            # The details drawer open, so the collapsed half is reviewable too.
            page.click(".disclosure__summary")
            page.wait_for_timeout(300)
            page.screenshot(path=OUT / f"entry-open-{scheme}.png", full_page=True)

            page.goto(f"{BASE}/settings/accounts")
            page.wait_for_timeout(400)
            page.screenshot(path=OUT / f"accounts-{scheme}.png")

            ctx.close()
        browser.close()
finally:
    server.terminate()
    server.wait(timeout=10)

print("shots written:", sorted(p.name for p in OUT.glob("*.png")))
