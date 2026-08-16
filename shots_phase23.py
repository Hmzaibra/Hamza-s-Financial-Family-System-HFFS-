"""Screenshot the Phase 2 and 3 screens at 380px, light and dark.

Separate from `shots.py` because the seed is different: this one needs a real
photo attached, budgets in three states (comfortable, warning, over), and a
second person to own a budget the reader may not see. `shots.py` stays the
picture of the entry form on a clean install.

The lesson this exists for is Phase 1's: the FX and transfer fields sat visible
on a plain spend for a whole session while sixty-one assertions passed, and it
was a screenshot that caught it. Assertions check what you thought to ask.
"""

import io
import os
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

PORT = 5075
BASE = f"http://127.0.0.1:{PORT}"
OUT = Path("shots-phase23")
OUT.mkdir(exist_ok=True)

tmpdir = Path(tempfile.mkdtemp())
db_path = tmpdir / "shots.db"
uploads = tmpdir / "uploads"
uploads.mkdir()

conn = dbmod.connect(db_path)
migrate(conn, Config.MIGRATIONS_DIR, log=lambda *_: None)
pw = generate_password_hash("shots-password")
conn.execute(
    "INSERT INTO users (id, username, display_name, password_hash, role, default_shared, "
    "timezone, telegram_chat_id, is_active, created_at) VALUES "
    "(1,'sam','Sam',?,'admin',1,'Africa/Cairo','10012',1,'t'),"
    "(2,'lea','Lea',?,'member',1,'Africa/Cairo',NULL,1,'t')", (pw, pw))
conn.execute(
    "INSERT INTO accounts (id, name, type, currency, owner_id, is_active, sort_order, created_at) "
    "VALUES (2,'CIB Current','bank','EGP',NULL,1,20,'t'),"
    "       (3,'Cash','cash','EGP',NULL,1,30,'t')")
# 004's trigger requires a handle on an Instapay row, and it links to the bank
# it draws on — the seed has to satisfy the same rules the form does.
conn.execute(
    "INSERT INTO accounts (id, name, type, currency, owner_id, parent_account_id, is_active, "
    "sort_order, created_at, instapay_handle) "
    "VALUES (1,'Sam - @sam_pay','instapay','EGP',NULL,2,1,10,'t','@sam_pay')")
conn.execute(
    "INSERT INTO merchants (id, name, default_category_id, default_is_online, "
    "default_account_id, is_system, is_active, created_at) VALUES "
    "(50,'Seoudi',1,0,2,0,1,'t'), (51,'Gad',102,0,3,0,1,'t'), (52,'Uber',112,1,2,0,1,'t'),"
    "(53,'Carrefour',1,0,2,0,1,'t'), (55,'Talabat',103,1,2,0,1,'t')")

for i, (mid, cat, amount) in enumerate(
        [(50, 1, 48250), (51, 102, 9900), (52, 112, 13500),
         (53, 1, 121000), (55, 103, 27400)]):
    conn.execute(
        "INSERT INTO transactions (user_id, occurred_on, direction, amount_minor, currency, "
        "account_id, merchant_id, category_id, is_online, is_shared, receiptless, "
        "created_at, updated_at) VALUES (1,?,'spend',?,'EGP',2,?,?,0,1,?,?,'t')",
        (f"2026-08-1{i}", amount, mid, cat, 1 if i == 1 else 0, f"2026-08-1{i}T10:00:00Z"))

# Three budgets, one of each state, so the bar's colours are all photographed.
conn.execute(
    "INSERT INTO limits (name, scope_type, scope_id, period, amount_minor, currency, "
    "warn_pct, is_active, created_at) VALUES "
    "('Groceries','category',1,'monthly',150000,'EGP',80,1,'t'),"
    "('Everything','household',NULL,'monthly',400000,'EGP',80,1,'t'),"
    "('Eating out','category',103,'weekly',20000,'EGP',75,1,'t'),"
    "('Lea''s pocket money','user',2,'monthly',80000,'EGP',80,1,'t')")
conn.commit()
conn.close()

env = {**os.environ, "FLASK_APP": "app", "DATABASE_PATH": str(db_path),
       "UPLOAD_DIR": str(uploads), "SECRET_KEY": "shots-key",
       "SESSION_COOKIE_SECURE": "0"}
server = subprocess.Popen([sys.executable, "-m", "flask", "run", "--port", str(PORT)],
                          env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
time.sleep(3)


def fake_receipt() -> bytes:
    """Something receipt-shaped, so the gallery is photographed with real tiles."""
    from PIL import Image, ImageDraw

    image = Image.new("RGB", (760, 1180), (247, 245, 240))
    draw = ImageDraw.Draw(image)
    draw.rectangle((60, 60, 700, 1120), outline=(200, 196, 188), width=3)
    draw.text((110, 120), "SEOUDI MARKET", fill=(40, 40, 40))
    y = 220
    for label, price in [("Bread", "12.00"), ("Milk 1L", "38.50"), ("Tomatoes", "22.75"),
                         ("Chicken", "185.00"), ("Rice 2kg", "94.00"), ("Eggs x10", "62.00")]:
        draw.text((110, y), label, fill=(70, 70, 70))
        draw.text((560, y), price, fill=(70, 70, 70))
        y += 58
    draw.line((110, y + 20, 650, y + 20), fill=(180, 176, 168), width=2)
    draw.text((110, y + 60), "TOTAL", fill=(20, 20, 20))
    draw.text((540, y + 60), "482.50", fill=(20, 20, 20))
    buffer = io.BytesIO()
    image.save(buffer, "JPEG", quality=88)
    return buffer.getvalue()


receipt_path = tmpdir / "receipt.jpg"
receipt_path.write_bytes(fake_receipt())

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

            # The till screen with the camera in its seat.
            page.wait_for_timeout(400)
            page.screenshot(path=OUT / f"entry-camera-{scheme}.png")

            # Mid-entry with a photo chosen: the tick state.
            page.fill("#amount", "482.50")
            page.click("label[for='m-50']")
            page.set_input_files("#receipt", str(receipt_path))
            page.wait_for_timeout(300)
            page.screenshot(path=OUT / f"entry-photo-{scheme}.png")

            page.click("button[type=submit].btn--save")
            page.wait_for_load_state("networkidle")
            page.wait_for_timeout(400)

            # The month screen with three budget bars.
            page.goto(f"{BASE}/dashboard")
            page.wait_for_load_state("networkidle")
            page.wait_for_timeout(400)
            page.screenshot(path=OUT / f"dashboard-budgets-{scheme}.png", full_page=True)

            # The list, showing which rows carry a photo.
            page.goto(f"{BASE}/transactions")
            page.wait_for_timeout(400)
            page.screenshot(path=OUT / f"entries-{scheme}.png")

            # The edit screen's gallery.
            page.click(".list__item")
            page.wait_for_load_state("networkidle")
            page.wait_for_timeout(500)
            page.screenshot(path=OUT / f"edit-gallery-{scheme}.png", full_page=True)

            page.goto(f"{BASE}/settings/limits")
            page.wait_for_timeout(400)
            page.screenshot(path=OUT / f"budgets-{scheme}.png", full_page=True)

            page.goto(f"{BASE}/settings/limits/new")
            page.wait_for_timeout(400)
            page.screenshot(path=OUT / f"budget-form-{scheme}.png", full_page=True)

            page.goto(f"{BASE}/settings/people")
            page.wait_for_timeout(400)
            page.screenshot(path=OUT / f"people-{scheme}.png")

            page.goto(f"{BASE}/settings/people/2")
            page.wait_for_timeout(400)
            page.screenshot(path=OUT / f"person-form-{scheme}.png", full_page=True)

            ctx.close()
        browser.close()
finally:
    server.terminate()
    server.wait(timeout=10)

print("shots written:", sorted(p.name for p in OUT.glob("*.png")))
