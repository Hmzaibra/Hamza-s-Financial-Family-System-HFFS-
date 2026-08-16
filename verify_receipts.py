"""Verification for receipt photos.

Two things here are worth more than the rest put together.

The first is that EXIF is actually gone. A photo of a pharmacy receipt carries
the coordinates of the pharmacy, and "we strip metadata" is the kind of claim
that stays true right up until someone changes the resize call. So the check
below writes a real GPS tag into a real JPEG, runs it through the real pipeline,
and reads the bytes back off disk.

The second is that deleting a transaction still records the files it left
behind. That path runs through a foreign-key CASCADE, and SQLite does not fire
delete triggers on a cascade unless recursive_triggers is on — a pragma that is
off by default and whose absence fails completely silently. The uploads folder
would just quietly grow forever.
"""

from __future__ import annotations

import io
import re
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import db as dbmod
import receipts
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


# ------------------------------------------------------------- test images


def jpeg_with_gps(size=(2400, 1800)) -> bytes:
    """A JPEG carrying the tags a phone camera actually writes.

    GPSInfo is the one that matters. Make, Model and DateTimeOriginal are here
    so the check can tell "metadata is gone" from "one tag is gone".
    """
    from PIL import Image

    image = Image.new("RGB", size, (210, 180, 140))
    exif = Image.Exif()
    exif[0x010F] = "TestPhone"                    # Make
    exif[0x0110] = "Model X"                      # Model
    exif[0x0132] = "2026:08:15 19:04:11"          # DateTime
    # Degrees, minutes, seconds — somewhere in Cairo, which is exactly the kind
    # of thing that has no business riding along with a pharmacy receipt.
    exif[0x8825] = {                              # GPSInfo
        1: "N", 2: (30.0, 2.0, 0.0),
        3: "E", 4: (31.0, 14.0, 0.0),
    }
    buffer = io.BytesIO()
    image.save(buffer, "JPEG", exif=exif.tobytes(), quality=90)
    return buffer.getvalue()


def rotated_jpeg() -> bytes:
    """Portrait pixels tagged 'rotate me 90°', which is how a phone stores one."""
    from PIL import Image

    image = Image.new("RGB", (400, 200), (30, 90, 80))
    exif = Image.Exif()
    exif[0x0112] = 6                              # Orientation: rotate 90 CW
    buffer = io.BytesIO()
    image.save(buffer, "JPEG", exif=exif.tobytes())
    return buffer.getvalue()


def image_bytes(fmt: str, size=(800, 600), mode="RGB", colour=(120, 140, 130)) -> bytes:
    from PIL import Image

    buffer = io.BytesIO()
    Image.new(mode, size, colour).save(buffer, fmt)
    return buffer.getvalue()


class Upload:
    """The smallest thing that behaves like a Werkzeug FileStorage."""

    def __init__(self, data: bytes, filename: str = "receipt.jpg"):
        self._data = data
        self.filename = filename

    def read(self) -> bytes:
        return self._data


def main() -> int:
    tmpdir = Path(tempfile.mkdtemp())

    class T(Config):
        DATABASE_PATH = tmpdir / "receipts.db"
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
        "INSERT INTO accounts (id, name, type, currency, opening_balance_minor, is_active, "
        "sort_order, created_at) VALUES (1,'CIB','bank','EGP',100000,1,10,'t')")
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

    def raw(q, p=()):
        """A connection with no pragmas set by this app, for pragma checks."""
        import sqlite3
        c = sqlite3.connect(T.DATABASE_PATH)
        try:
            return c.execute(q, p).fetchall()
        finally:
            c.close()

    def txn(**kw) -> int:
        cols = {
            "user_id": 1, "occurred_on": "2026-08-10", "direction": "spend",
            "amount_minor": 5000, "currency": "EGP", "fx_rate_to_base": None,
            "account_id": 1, "counter_account_id": None, "counter_amount_minor": None,
            "counter_currency": None, "merchant_id": 60, "category_id": None,
            "is_online": 0, "note": None, "is_shared": 1, "receiptless": 0,
            "created_at": "2026-08-10T00:00:00Z", "updated_at": "2026-08-10T00:00:00Z",
        }
        cols.update(kw)
        c = dbmod.connect(T.DATABASE_PATH)
        try:
            cur = c.execute(
                f"INSERT INTO transactions ({','.join(cols)}) "
                f"VALUES ({','.join('?' for _ in cols)})", tuple(cols.values()))
            c.commit()
            return cur.lastrowid
        finally:
            c.close()

    def login(client, username="admin"):
        page = client.get("/login").data
        client.post("/login", data={"username": username, "password": "pw12345678",
                                    "_csrf": token(page)})

    # ------------------------------------------------------------ migration

    print("\nmigration 005")
    names = {r["name"] for r in sql("SELECT name FROM sqlite_master")}
    check("orphaned_files exists", "orphaned_files" in names)
    check("a receiptless entry cannot take an attachment",
          "trg_attachments_not_receiptless" in names)
    check("and the flag cannot be set on one that has photos",
          "trg_transactions_receiptless_conflict" in names)
    check("a deleted attachment records what it left on disk", "trg_attachments_orphan" in names)
    check("two rows cannot point at one file", "ux_attachments_file" in names)
    check("recursive triggers are on, or the cascade above never fires",
          dbmod.connect(T.DATABASE_PATH).execute("PRAGMA recursive_triggers").fetchone()[0] == 1)
    check("and they are off without our pragma — which is the point of setting it",
          raw("PRAGMA recursive_triggers")[0][0] == 0)

    # ---------------------------------------------------------------- EXIF

    print("\nthe metadata a phone camera writes does not survive")
    with app.app_context(), app.test_request_context():
        one = txn()
        stored = receipts.store(one, Upload(jpeg_with_gps()))
        on_disk = receipts.absolute(stored["file_path"]).read_bytes()

        from PIL import Image
        reopened = Image.open(io.BytesIO(on_disk))
        tags = dict(reopened.getexif() or {})

        check("the source really had GPS on it, or this check proves nothing",
              0x8825 in dict(Image.open(io.BytesIO(jpeg_with_gps())).getexif()))
        check("no GPS survives", 0x8825 not in tags)
        check("no camera make or model", 0x010F not in tags and 0x0110 not in tags)
        check("no capture timestamp", 0x0132 not in tags)
        check("in fact no EXIF block at all", not tags)
        check("and no APP1 marker in the raw bytes either", b"Exif\x00\x00" not in on_disk)

    print("\norientation is spent on the pixels before it is thrown away")
    with app.app_context(), app.test_request_context():
        two = txn()
        stored = receipts.store(two, Upload(rotated_jpeg()))
        from PIL import Image
        out = Image.open(receipts.absolute(stored["file_path"]))
        # 400x200 tagged "rotate 90" is a portrait photo. Strip the tag without
        # applying it and it stays landscape — and every receipt shot in portrait
        # is stored on its side, permanently.
        check("a sideways photo comes out upright", out.height > out.width)

    # ------------------------------------------------------------- resizing

    print("\nbig photos are made small, because a Pi is serving them over a phone")
    with app.app_context(), app.test_request_context():
        three = txn()
        stored = receipts.store(three, Upload(jpeg_with_gps((3600, 2400))))
        from PIL import Image
        full = Image.open(receipts.absolute(stored["file_path"]))
        thumb = Image.open(receipts.absolute(stored["thumb_path"]))
        check("the long edge lands on the cap", max(full.size) == receipts.MAX_EDGE)
        check("the aspect ratio is kept", abs(full.width / full.height - 1.5) < 0.01)
        check("a thumbnail is made too", max(thumb.size) == receipts.THUMB_EDGE)
        check("the thumbnail is much the smaller file",
              receipts.absolute(stored["thumb_path"]).stat().st_size
              < receipts.absolute(stored["file_path"]).stat().st_size)
        check("byte_size records the file that was actually written",
              stored["byte_size"] == receipts.absolute(stored["file_path"]).stat().st_size)
        check("a 3600px original shrinks by an order of magnitude",
              stored["byte_size"] < len(jpeg_with_gps((3600, 2400))) // 4)

    print("\nformats")
    with app.app_context(), app.test_request_context():
        png = receipts.store(txn(), Upload(image_bytes("PNG"), "shot.png"))
        check("a PNG stays a PNG — a bank screenshot is text, and JPEG smears it",
              png["mime"] == "image/png" and png["file_path"].endswith(".png"))

        bmp = receipts.store(txn(), Upload(image_bytes("BMP"), "scan.bmp"))
        check("anything else is re-encoded as JPEG rather than refused",
              bmp["mime"] == "image/jpeg")

        clear = receipts.store(txn(), Upload(image_bytes("WEBP", mode="RGBA",
                                                         colour=(0, 0, 0, 0)), "x.webp"))
        from PIL import Image
        flat = Image.open(receipts.absolute(clear["file_path"]))
        check("transparency is flattened onto white, not onto black",
              flat.mode == "RGB" and flat.getpixel((5, 5)) == (255, 255, 255))

        try:
            receipts.store(txn(), Upload(b"this is not an image at all", "note.txt"))
            check("a file that is not an image is refused", False)
        except receipts.ReceiptError as exc:
            check("a file that is not an image is refused", True)
            check("and the message names HEIC, which is what an iPhone actually sends",
                  "HEIC" in str(exc))

        try:
            receipts.store(txn(), Upload(b"", "empty.jpg"))
            check("an empty file is refused", False)
        except receipts.ReceiptError:
            check("an empty file is refused", True)

    # ---------------------------------------------------------- the conflict

    print("\na photo and 'there was no receipt' cannot both be true")
    with app.app_context(), app.test_request_context():
        flagged = txn(receiptless=1)
        receipts.store(flagged, Upload(image_bytes("JPEG")))
        check("attaching a photo clears the flag — the photo is evidence",
              sql("SELECT receiptless FROM transactions WHERE id = ?", (flagged,))[0][0] == 0)

        # Straight at the database, past every line of Python.
        still = txn(receiptless=1)
        c = dbmod.connect(T.DATABASE_PATH)
        try:
            c.execute(
                "INSERT INTO attachments (transaction_id, file_path, thumb_path, mime, "
                "byte_size, created_at) VALUES (?,?,?,?,?,?)",
                (still, "x/y.jpg", "x/y-t.jpg", "image/jpeg", 10, "t"))
            check("and the database refuses the contradiction on its own", False)
        except Exception as exc:
            check("and the database refuses the contradiction on its own",
                  "no receipt" in str(exc))
        finally:
            c.close()

        photographed = txn()
        receipts.store(photographed, Upload(image_bytes("JPEG")))
        try:
            txns._prepare({"id": 1, "timezone": "Africa/Cairo", "default_shared": 1,
                           "role": "admin"},
                          {"amount": "50", "direction": "spend", "account_id": "1",
                           "receiptless": "1"},
                          exclude_id=photographed)
            check("ticking 'no receipt' on an entry with photos is refused", False)
        except txns.EntryError as exc:
            check("ticking 'no receipt' on an entry with photos is refused", True)
            check("with a sentence that says what to do about it",
                  "Remove" in str(exc) and exc.field == "receiptless")

        c = dbmod.connect(T.DATABASE_PATH)
        try:
            c.execute("UPDATE transactions SET receiptless = 1 WHERE id = ?", (photographed,))
            check("the database refuses that one too", False)
        except Exception:
            check("the database refuses that one too", True)
        finally:
            c.close()

        # The trigger fires on any UPDATE that *names* the column, and
        # _prepare() rewrites every column on every edit — so an ordinary save
        # on a photographed entry has to still work.
        txns.update_transaction(
            photographed,
            {"id": 1, "timezone": "Africa/Cairo", "default_shared": 1, "role": "admin"},
            {"amount": "77", "direction": "spend", "account_id": "1", "currency": "EGP",
             "occurred_on": "2026-08-11", "is_shared": "1"})
        check("but an ordinary edit of a photographed entry still saves",
              sql("SELECT amount_minor FROM transactions WHERE id = ?",
                  (photographed,))[0][0] == 7700)

    # -------------------------------------------------------------- orphans

    print("\nthe filesystem is never left holding a file nothing points at")
    with app.app_context(), app.test_request_context():
        four = txn()
        stored = receipts.store(four, Upload(image_bytes("JPEG")))
        full_path = receipts.absolute(stored["file_path"])
        thumb_path = receipts.absolute(stored["thumb_path"])
        check("the files are on disk to begin with", full_path.is_file() and thumb_path.is_file())

        receipts.remove(stored["id"])
        check("removing a photo unlinks both files",
              not full_path.exists() and not thumb_path.exists())
        check("and leaves nothing owed", sql("SELECT COUNT(*) FROM orphaned_files")[0][0] == 0)

        five = txn()
        kept = receipts.store(five, Upload(image_bytes("JPEG")))
        kept_full = receipts.absolute(kept["file_path"])

        # The cascade path. This is the one the pragma exists for.
        dbmod.execute("DELETE FROM transactions WHERE id = ?", (five,))
        owed = sql("SELECT path FROM orphaned_files ORDER BY path")
        check("deleting a transaction records the files its photos left behind",
              len(owed) == 2)
        check("the row is gone with it",
              sql("SELECT COUNT(*) FROM attachments WHERE id = ?", (kept["id"],))[0][0] == 0)
        check("the file is still there until something pays the debt", kept_full.is_file())

        gone = receipts.reap()
        check("reap unlinks them", gone == 2 and not kept_full.exists())
        check("and clears the debt", sql("SELECT COUNT(*) FROM orphaned_files")[0][0] == 0)

        # A path whose file has already vanished — a half-finished restore, or a
        # second reap racing the first.
        dbmod.execute("INSERT INTO orphaned_files (path, orphaned_at) VALUES (?,?)",
                      ("2026/08/nothing-here.jpg", "t"))
        receipts.reap()
        check("a file that is already gone is still a debt paid, not a retry loop",
              sql("SELECT COUNT(*) FROM orphaned_files")[0][0] == 0)

        check("emptied month folders are tidied away rather than left as a husk",
              not any(p.is_dir() and not any(p.iterdir())
                      for p in (T.UPLOAD_DIR).glob("*/*")))

    print("\na stored path cannot walk out of the uploads folder")
    with app.app_context(), app.test_request_context():
        for attempt in ("../../etc/passwd", "/etc/passwd", "2026/../../../secret"):
            try:
                receipts.absolute(attempt)
                check(f"{attempt!r} is refused", False)
            except receipts.ReceiptError:
                check(f"{attempt!r} is refused", True)
        check("uploads sits outside static/, so Flask never serves it by filename",
              "static" not in str(Path(T.UPLOAD_DIR).resolve()).replace(str(tmpdir), ""))

    # --------------------------------------------------------- the web side

    print("\nwho may look at a photograph of what someone bought")
    with app.app_context(), app.test_request_context():
        mine_private = txn(user_id=1, is_shared=0)
        mine_shared = txn(user_id=1, is_shared=1)
        private_att = receipts.store(mine_private, Upload(image_bytes("JPEG")))
        shared_att = receipts.store(mine_shared, Upload(image_bytes("JPEG")))

    with app.test_client() as c:
        r = c.get(f"/receipts/{private_att['id']}")
        check("signed out, a receipt is a redirect to the login page",
              r.status_code == 302 and "/login" in r.headers["Location"])

    with app.test_client() as c:
        login(c, "mem")
        check("a member cannot fetch a photo from someone's private entry",
              c.get(f"/receipts/{private_att['id']}").status_code == 404)
        check("404 rather than 403 — that it exists is part of what is hidden",
              c.get(f"/receipts/{private_att['id']}").status_code != 403)
        shared = c.get(f"/receipts/{shared_att['id']}")
        check("but a shared entry's photo is theirs to see", shared.status_code == 200)
        check("served as the type we re-encoded it to",
              shared.headers["Content-Type"].startswith("image/jpeg"))
        check("cached privately, never in a shared cache",
              "private" in shared.headers.get("Cache-Control", ""))
        thumb = c.get(f"/receipts/{shared_att['id']}/thumb")
        check("the thumbnail is its own URL and is smaller",
              thumb.status_code == 200 and len(thumb.data) < len(shared.data))
        check("a photo that does not exist is a 404",
              c.get("/receipts/999999").status_code == 404)

    with app.test_client() as c:
        login(c, "mem")
        # The token comes from the entry form: the list is a GET form and
        # carries none, so a token lifted from there would make this check pass
        # for the wrong reason — a 400 rather than the 403 being tested for.
        page = c.get("/").data
        r = c.post(f"/receipts/{shared_att['id']}/delete", data={"_csrf": token(page)})
        check("seeing a shared photo never conferred the right to delete it",
              r.status_code == 403)
        check("and the photo is still there", sql(
            "SELECT COUNT(*) FROM attachments WHERE id = ?",
            (shared_att["id"],))[0][0] == 1)

    print("\nthe camera on the entry form")
    with app.test_client() as c:
        login(c)
        page = c.get("/").data
        check("the form posts multipart, or the file never arrives",
              b'enctype="multipart/form-data"' in page)
        check("the camera sits in the seat Phase 0 reserved for it",
              b'class="pos__aux"' in page and b'name="receipt"' in page)
        check("it is a label wrapping a file input, so it works with JS off",
              b'<label for="receipt" class="camera"' in page)
        check("and asks a phone for the back camera", b'capture="environment"' in page)

        r = c.post("/", data={
            "_csrf": token(page), "amount": "42.50", "direction": "spend",
            "account_id": "1", "currency": "EGP", "occurred_on": "2026-08-12",
            "receiptless": "1",
            "receipt": (io.BytesIO(jpeg_with_gps((900, 700))), "till.jpg"),
        }, content_type="multipart/form-data")
        check("a photo taken at the till posts with the entry", r.status_code == 302)

        newest = sql("SELECT id, receiptless FROM transactions ORDER BY id DESC LIMIT 1")[0]
        check("it is attached", sql(
            "SELECT COUNT(*) FROM attachments WHERE transaction_id = ?",
            (newest["id"],))[0][0] == 1)
        check("and ticking 'no receipt' alongside it loses to the photograph",
              newest["receiptless"] == 0)

        r = c.post("/", data={
            "_csrf": token(page), "amount": "9", "direction": "spend",
            "account_id": "1", "currency": "EGP", "occurred_on": "2026-08-12",
            "receipt": (io.BytesIO(b"not an image"), "note.txt"),
        }, content_type="multipart/form-data", follow_redirects=True)
        check("a photo that will not decode never costs you the purchase",
              b"did not attach" in r.data)
        check("and the entry is saved anyway",
              sql("SELECT amount_minor FROM transactions ORDER BY id DESC LIMIT 1")[0][0] == 900)

    print("\nthe gallery on the edit screen")
    with app.test_client() as c:
        login(c)
        page = c.get(f"/transactions/{mine_shared}/edit").data
        check("the photo is shown as a thumbnail", b'class="gallery__img"' in page)
        check("linking to the full-size one", b'class="gallery__link"' in page)

        # Follow the URLs the template actually built, rather than typing them
        # out here. Hand-typed URLs are how the gallery shipped once pointing at
        # /receipts/N/thumb/thumb — every check passed and every image was
        # broken, because the checks asked the routes and the page asked
        # url_for.
        srcs = re.findall(rb'src="(/receipts/[^"]+)"', page)
        hrefs = re.findall(rb'href="(/receipts/\d+)"', page)
        check("the thumbnail URL on the page is one the app answers",
              srcs and all(c.get(u.decode()).status_code == 200 for u in srcs))
        check("and so is the full-size link",
              hrefs and all(c.get(u.decode()).status_code == 200 for u in hrefs))
        check("they are not the same URL, or the gallery is loading full-size photos",
              set(srcs) != set(hrefs))
        check("with a remove button of its own", b"Remove" in page)
        check("the 'no receipt' box is gone rather than sitting there to be refused",
              b'name="receiptless"' not in page)
        check("and says why", b"cannot be marked as having none" in page)

        empty = txn()
        page = c.get(f"/transactions/{empty}/edit").data
        check("an entry with no photo still offers the box", b'name="receiptless"' in page)
        check("and an upload field", b'type="file"' in page)

        r = c.post(f"/receipts/transactions/{empty}", data={
            "_csrf": token(page),
            "receipt": (io.BytesIO(image_bytes("JPEG")), "later.jpg"),
        }, content_type="multipart/form-data", follow_redirects=True)
        check("a photo can be added afterwards", b"Photo added" in r.data)

    print("\nthe list says which entries have one")
    with app.test_client() as c:
        login(c)
        page = c.get("/transactions").data
        check("a photographed entry says so", b"photo" in page)
        check("a receiptless one still says that", b"no receipt" in page)

    print("\ntoo large a photo is a sentence, not a Werkzeug page")
    with app.test_client() as c:
        login(c)
        page = c.get("/").data
        r = c.post("/", data={
            "_csrf": token(page), "amount": "5", "direction": "spend", "account_id": "1",
            "receipt": (io.BytesIO(b"x" * (T.MAX_CONTENT_LENGTH + 1024)), "huge.jpg"),
        }, content_type="multipart/form-data")
        check("the size cap is enforced", r.status_code == 413)
        check("and says what to do about it", b"lower resolution" in r.data)

    print("\nthe rules that only reading the source can check")
    source = Path("blueprints").glob("*.py")
    leaks = [p.name for p in source if "urllib" in p.read_text(encoding="utf-8")]
    check("no blueprint opens a socket — invariant 7", not leaks)
    css = Path("static/css/app.css").read_text(encoding="utf-8")
    check("the camera has a focus ring, since the input it stands for is hidden",
          "input:focus-visible + .camera" in css)

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
