"""Auth flow verification against the real app, using Flask's test client."""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import re

import db as dbmod
from app import create_app
from config import Config
from migrate import migrate
from werkzeug.security import generate_password_hash

failures: list[str] = []


def token(html: bytes) -> str:
    """CSRF token from a rendered page. Phase 1 protects every unsafe method."""
    m = re.search(rb'name="_csrf" value="([^"]+)"', html)
    return m.group(1).decode() if m else ""


def check(label: str, condition: bool) -> None:
    print(f"{'  ok  ' if condition else '  FAIL'}  {label}")
    if not condition:
        failures.append(label)


def main() -> int:
    tmpdir = Path(tempfile.mkdtemp())

    class T(Config):
        DATABASE_PATH = tmpdir / "auth.db"
        UPLOAD_DIR = tmpdir / "uploads"
        SECRET_KEY = "test-key-not-a-real-one"
        LOGIN_MAX_FAILS_PER_USER = 3
        LOGIN_MAX_FAILS_PER_IP = 50
        LOGIN_WINDOW_MINUTES = 15
        SESSION_COOKIE_SECURE = False

    conn = dbmod.connect(T.DATABASE_PATH)
    migrate(conn, Config.MIGRATIONS_DIR, log=lambda *_: None)
    conn.execute(
        "INSERT INTO users (id, username, display_name, password_hash, role, "
        "default_shared, timezone, is_active, created_at) VALUES "
        "(1,'sam','Sam',?,'admin',1,'Africa/Cairo',1,'2026-08-15T00:00:00Z'),"
        "(2,'gone','Gone',?,'member',1,'Africa/Cairo',0,'2026-08-15T00:00:00Z')",
        (generate_password_hash("correct-horse"), generate_password_hash("correct-horse")),
    )
    conn.commit()
    conn.close()

    app = create_app(T)
    app.config.update(TESTING=True)

    print("\nsession protection")
    with app.test_client() as c:
        r = c.get("/")
        check("anonymous / redirects to login", r.status_code == 302 and "/login" in r.headers["Location"])
        r = c.get("/login")
        check("login page renders", r.status_code == 200 and b"Sign in" in r.data)

    print("\ncredentials")
    with app.test_client() as c:
        r = c.post("/login", data={"_csrf": token(c.get("/login").data), "username": "sam", "password": "wrong"})
        check("wrong password → 401", r.status_code == 401)
        check("no username enumeration in the message",
              b"Wrong username or password" in r.data)

        r = c.post("/login", data={"_csrf": token(c.get("/login").data), "username": "nobody", "password": "wrong"})
        check("unknown user gives the identical message",
              r.status_code == 401 and b"Wrong username or password" in r.data)

        r = c.post("/login", data={"_csrf": token(c.get("/login").data), "username": "gone", "password": "correct-horse"})
        check("deactivated user cannot sign in", r.status_code == 401)

    with app.test_client() as c:
        r = c.post("/login", data={"_csrf": token(c.get("/login").data), "username": "sam", "password": "correct-horse"})
        check("correct password redirects", r.status_code == 302)
        r = c.get("/", follow_redirects=True)
        check("entry form renders after login", r.status_code == 200 and b"pos__amount" in r.data)
        check("display name shown in topbar", b"Sam" in r.data)

        r = c.post("/logout", data={"_csrf": token(c.get("/").data)})
        check("logout redirects to login", r.status_code == 302 and "/login" in r.headers["Location"])
        r = c.get("/")
        check("entry form protected again after logout", r.status_code == 302)

    print("\ncase-insensitive username")
    with app.test_client() as c:
        r = c.post("/login", data={"_csrf": token(c.get("/login").data), "username": "SAM", "password": "correct-horse"})
        check("username matching is case-insensitive", r.status_code == 302)

    def reset_attempts() -> None:
        """Blocks above deliberately fail logins, which counts toward the lockout.
        Each block below starts from a clean slate so it tests one thing."""
        conn = dbmod.connect(T.DATABASE_PATH)
        conn.execute("DELETE FROM login_attempts")
        conn.commit()
        conn.close()

    print("\nrate limiting")
    reset_attempts()
    with app.test_client() as c:
        codes = [
            c.post("/login", data={"_csrf": token(c.get("/login").data), "username": "sam", "password": "nope"}).status_code
            for _ in range(3)
        ]
        check("first 3 failures return 401", codes == [401, 401, 401])
        r = c.post("/login", data={"_csrf": token(c.get("/login").data), "username": "sam", "password": "nope"})
        check("4th attempt is locked out (429)", r.status_code == 429)
        r = c.post("/login", data={"_csrf": token(c.get("/login").data), "username": "sam", "password": "correct-horse"})
        check("correct password still refused while locked out", r.status_code == 429)
        r = c.post("/login", data={"_csrf": token(c.get("/login").data), "username": "someoneelse", "password": "nope"})
        check("lockout is per-username, not global", r.status_code == 401)

    print("\ncookie and headers")
    reset_attempts()
    with app.test_client() as c:
        r = c.post("/login", data={"_csrf": token(c.get("/login").data), "username": "sam", "password": "correct-horse"},
                   base_url="http://localhost")
        cookie = r.headers.get("Set-Cookie", "")
        check("session cookie is HttpOnly", "HttpOnly" in cookie)
        check("session cookie is SameSite=Lax", "SameSite=Lax" in cookie)
        check("Secure flag off for plain http dev", "Secure" not in cookie)

        r = c.get("/")
        check("CSP present and self-only", "default-src 'self'" in r.headers.get("Content-Security-Policy", ""))
        check("X-Frame-Options DENY", r.headers.get("X-Frame-Options") == "DENY")
        check("nosniff set", r.headers.get("X-Content-Type-Options") == "nosniff")

    print("\nopen redirect")
    reset_attempts()
    with app.test_client() as c:
        r = c.post("/login?next=https://evil.example/x",
                   data={"_csrf": token(c.get("/login").data), "username": "sam", "password": "correct-horse"})
        check("absolute next= is ignored", r.headers["Location"] in ("/", "http://localhost/"))
    with app.test_client() as c:
        r = c.post("/login?next=//evil.example/x",
                   data={"_csrf": token(c.get("/login").data), "username": "sam", "password": "correct-horse"})
        check("protocol-relative next= is ignored", r.headers["Location"] in ("/", "http://localhost/"))

    print("\n404")
    reset_attempts()
    with app.test_client() as c:
        c.post("/login", data={"_csrf": token(c.get("/login").data), "username": "sam", "password": "correct-horse"})
        r = c.get("/no-such-page")
        check("404 renders the styled page", r.status_code == 404 and b"404" in r.data)

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
