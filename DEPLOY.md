# Running it for real

Two machines are described here: the **laptop**, which is where it runs now,
and the **Pi**, which is where it goes later. The app is the same on both. What
changes is who starts it, who schedules the four jobs, and whether the process
survives closing the lid.

Everything the app needs lives in one folder. Moving it is `rsync` and this
document — there is no state anywhere else on the machine.

---

## What is in front of it

Nothing on the internet. The app binds to `127.0.0.1` and `tailscale serve`
puts it on the tailnet with a real TLS certificate, which is what makes the
phones' `https://` work without a certificate warning and without opening a
port on the router.

```bash
tailscale serve --bg 8000            # https://<machine>.<tailnet>.ts.net → 127.0.0.1:8000
tailscale serve status               # what is actually being served
tailscale serve --https=443 off      # stop
```

Do **not** run `tailscale funnel`. That publishes the same URL to the whole
internet; `serve` keeps it to devices signed into the tailnet.

Because that is real HTTPS, set this in `.env` on any machine reached this way:

```
SESSION_COOKIE_SECURE=1
```

The one thing to know about that flag: with it on, signing in over plain
`http://localhost:5000` silently fails. The cookie is set and never sent back,
so the login page reloads with no error. If a dev session starts doing that,
this is why.

---

## The laptop (now)

Windows, from the project folder.

```powershell
.venv\Scripts\python.exe -m pip install -r requirements.txt
.venv\Scripts\python.exe -m pip install waitress
.venv\Scripts\python.exe -m flask --app app migrate
```

Gunicorn does not run on Windows — it forks. Waitress does, is one file of
configuration, and is enough for four phones:

```powershell
.venv\Scripts\python.exe -m waitress --listen=127.0.0.1:8000 --threads=6 "app:create_app()"
```

Then `tailscale serve --bg 8000` in another window, once. The serve config
persists across reboots; the waitress command does not — that is the honest
limit of the laptop-now path. Closing the lid stops the app, and the phones get
the offline page until it is started again.

The four scheduled jobs are registered once, from an elevated PowerShell:

```powershell
Set-ExecutionPolicy -Scope Process Bypass
.\deploy\windows-tasks.ps1
```

See `deploy/windows-tasks.ps1` for what each one does and how to remove them.

---

## The Pi (later)

Raspberry Pi OS Lite, 64-bit. Every dependency has a prebuilt wheel, so nothing
compiles.

```bash
sudo apt update && sudo apt install -y python3-venv sqlite3
git clone <this repo> /home/pi/expenses && cd /home/pi/expenses
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt gunicorn
cp .env.example .env
python3 -c "import secrets; print(secrets.token_hex(32))"   # paste into SECRET_KEY
```

Set `SESSION_COOKIE_SECURE=1` in `.env`, then:

```bash
.venv/bin/flask --app app migrate
.venv/bin/flask --app app create-admin        # only if this is a fresh database
sudo cp deploy/expenses.service /etc/systemd/system/expenses.service
sudo systemctl daemon-reload
sudo systemctl enable --now expenses
systemctl status expenses
```

The unit runs `flask migrate` before gunicorn every time it starts, so a
deployment is:

```bash
cd /home/pi/expenses && git pull && sudo systemctl restart expenses
```

Then the jobs — `crontab -e` as `pi`, and paste from `deploy/crontab.example`.

### Moving the data across

The database and the receipts are two files and a folder. Stop the app on both
ends first; copying a live SQLite file captures a torn page and misses whatever
is still in the `-wal`.

```bash
# on the laptop
.venv\Scripts\python.exe scripts\backup.py --dest backups

# then, from the Pi
rsync -av <laptop>:/path/to/backups/<stamp>/ /home/pi/expenses/restore/
cd /home/pi/expenses
cp restore/app.db app.db && rm -f app.db-wal app.db-shm
tar xzf restore/uploads.tar.gz
sqlite3 app.db "PRAGMA integrity_check;"      # must say: ok
.venv/bin/flask --app app migrate
```

---

## The four jobs

Same four on both machines; only the scheduler differs. Full reasoning is in
`deploy/crontab.example`.

| Job | When | If it stops running |
| --- | --- | --- |
| `fetch-rates` | daily 04:10 | Foreign-currency entries keep working from the cached rate. But `transactions.py` refuses a cross-currency transfer whose sides disagree by more than tenfold *by comparing against that cache* — an empty or ancient cache makes the guard silent, not strict. |
| `check-limits` | hourly | Budgets still track and still show on the Month screen. Nothing is sent. |
| `sweep-uploads` | weekly Sun 03:30 | Receipt JPEGs whose rows are deleted stay on disk. The database is still correct; the card slowly fills. |
| `backup.py` | daily 02:00 | One SD card is the only copy of the household's ledger. |

`check-limits` and `fetch-rates` are the only two things in the codebase that
open a socket, and both are CLI-only by design (invariant 7). Nothing reaches
the network during a request, so a dead provider can never hang the entry form.

---

## Checking it is actually working

```bash
systemctl status expenses                     # Pi
journalctl -u expenses -n 50 --no-pager
journalctl -t expenses --since yesterday      # the cron jobs, all four
tailscale serve status
curl -sI http://127.0.0.1:8000/login | head -1
```

From a phone on the tailnet, open the `https://…ts.net` URL, sign in, then use
the browser's **Add to Home Screen**. It installs as a standalone app: no
address bar, its own icon, its own entry in the app switcher.

What that install does *not* do is work offline. The service worker caches one
page — the one that says the server is unreachable — and nothing else, on
purpose: a stale balance looks exactly like a live one, and there is no way for
someone holding a phone to tell the difference. `static/js/sw.js` says so at
more length.

## When something is wrong

**A page says "the database is behind the code".** Run
`flask --app app migrate`. That page is the app noticing on purpose rather than
a view failing three frames deep; it re-checks on every request, so the page
clears without a restart.

**Login does nothing, no error.** `SESSION_COOKIE_SECURE=1` while reaching it
over plain http. Either use the tailnet https URL or set the flag back to 0.

**The phones cannot reach it, the laptop can.** `tailscale status` on both
ends. On the phone, check the Tailscale app is connected — iOS drops the VPN on
some network changes and does not say so.

**Everything is slow after a big import.** `sqlite3 app.db "PRAGMA
optimize; VACUUM;"` with the app stopped. This is a once-a-year thing, not
maintenance.

**A worker keeps restarting.** `journalctl -u expenses -f` and look for the
Python traceback above the restart line. `Restart=on-failure` means a crash
loop is quiet from the outside — the URL keeps working — so the log is the only
place it shows.
