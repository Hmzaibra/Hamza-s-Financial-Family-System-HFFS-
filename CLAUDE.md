# Working on this codebase

Self-hosted household expense tracker. Flask + SQLite, no build step, no CDN, no
framework beyond Flask. Read `README.md` for the model and `PHASE-1-NOTES.md` for
why things are the way they are before proposing to change them.

## Commands

```bash
.venv/Scripts/python.exe -m flask --app app run --debug   # Windows dev server
.venv/bin/flask --app app run --debug                     # POSIX
```

Verification — run **all seven** after any change; they build throwaway
databases and never touch `app.db` or `uploads/`:

```bash
python verify_phase0.py && python verify_auth.py && python verify_phase1.py && \
  python verify_accounts.py && python verify_balances.py && \
  python verify_receipts.py && python verify_limits.py
```

492 checks at time of writing. A failing check is a real regression or a rule that
changed on purpose — if the latter, update the check *and* say so, never delete it.

Several are *source* checks rather than behavioural ones: no `date.today()` in
request-path code, the stubbed `fx.fetch()` inversion, no blueprint importing
`telegram`, and `limits.py` not importing it at module scope. Each guards a
failure that is invisible at runtime until the numbers are already wrong or the
entry form is already hanging on a socket.

`shots.py` and `shots_phase23.py` photograph the screens at 380px in both colour
schemes. Run them after touching a template or the stylesheet. Every visual bug
this project has shipped was found in a screenshot with every assertion passing —
the FX fields showing on a plain spend, the camera button hanging off the edge of
the till panel, the gallery requesting `/receipts/6/thumb/thumb`. Assertions
check what you thought to ask.

## Invariants — do not break these without saying so out loud

1. **Money is integer minor units.** `Decimal` only inside `money.py`; no function
   there returns a float. Never `SUM(amount_minor * fx_rate_to_base)` in SQL —
   convert in Python via `money.convert_to_base()`.
2. **Amounts are positive**; `direction` carries the sign. Only a credit card may
   sit below zero.
3. **Dates are the *user's* local calendar date** (`db.today_for(tz)`); timestamps
   are UTC `YYYY-MM-DDTHH:MM:SSZ`. This binds anything that compares against
   "today" — card expiry included, on every screen that checks it, or a card
   reads live on one and expired on the other.
4. **Every transaction read goes through `visibility.visibility_sql()`**, which
   fails closed with no session. There are exactly **two** stated exceptions,
   account balances and budget figures; `balances.py` and `limits.py` each argue
   for their own in their docstrings, and nothing else may claim either. The
   shared argument is that a filtered aggregate over the household is a *wrong*
   number, not a partial one. In `limits.py` what section 4 filters instead is
   which budgets a person may see at all (`visible_limits()`), never the
   arithmetic inside one.
5. **Every transaction write goes through `transactions._prepare()`.** Create
   and edit both call it, so an edit cannot pass a check an insert would fail.
   DB constraints are the backstop, not the guard.
6. **Every unsafe request carries a CSRF token.** No exemption list.
7. **Nothing hits the network during a request.** `fx.py` and `telegram.py` are
   the only two modules that open a socket, and both are reached only from CLI
   commands (`fetch-rates`, `check-limits`, `telegram-chats`). `limits.py`
   imports `telegram` *inside* `sweep()`, because the dashboard imports
   `limits` on every page view.
8. **No inline `style=` or `on*=` attributes.** The CSP has no `unsafe-inline`, so
   they silently do not render. User-supplied colours go in SVG `fill` attributes.
9. **Migrations are immutable once applied.** Add a new numbered file; never edit
   `001`–`005`. The runner reports a changed checksum rather than re-running.
10. **The entry form works with JavaScript off.** JS may only remove waiting.
    Anything it enforces must also be enforced server-side. This is why the
    camera is a `<label>` wrapping a hidden file input rather than a button,
    why a budget's scope is one `<select>` carrying `category:7` rather than a
    kind and an id in two controls, and why "are you sure?" is a page rather
    than a `confirm()`. Note that `entry.js` no longer reads an empty account
    select as "untouched" — the box always names an account, and a `touched`
    flag carries that meaning instead.
11. **Files on disk are never deleted without the database knowing first.**
    `ON DELETE CASCADE` does not touch the filesystem, so an `AFTER DELETE`
    trigger records the debt in `orphaned_files` and `receipts.reap()` pays it.
    That trigger only fires on a cascade because `db.py` sets
    `PRAGMA recursive_triggers = ON` — off by default, silent when missing.

## Conventions

- Comments explain *why*, not *what*. Match the density of the surrounding file —
  this codebase is heavily commented on purpose and a bare patch reads as foreign.
- Errors shown to a person are sentences they can act on, not stack traces or
  constraint names. `EntryError(message, field)` re-renders the form with what
  they typed intact.
- Nothing dead ships: no unused helpers, no placeholder controls that lie about
  what they do. A reserved *space* with a comment is fine (see `.pos__aux`).
- New rules get enforced twice: in Python for the sentence, in a trigger or CHECK
  so nothing can write around it.
- Tests are `verify_*.py` in the house style — `check("plain english", condition)`,
  grouped under `print()` headings. They are documentation that runs.

## Where things live

`transactions.py` is the write path and the entry form's queries. `balances.py`
is the balance arithmetic and the month figures. `ledger.py` is the list, edit
and delete. `reference.py` is admin CRUD for accounts, categories, merchants,
people and budgets. `receipts.py` is the image pipeline and orphan cleanup;
`blueprints/receipts.py` is the only way a photo leaves the server. `limits.py`
is budget maths and the alert sweep. `fx.py` is the rate cache and `telegram.py`
is the sender — the two files that touch the internet. `visibility.py` is the
section 4 rule, implemented once. `money.py` owns every currency-shaped
decision.

## Current state

**Phases 0 through 3 are complete.** Balances, the month breakdown, the entry
list, receipt photos, and budgets that warn over Telegram are all in.

Phase 4 is reporting, CSV, a PWA manifest, and deployment: gunicorn behind
`tailscale serve`, with `SESSION_COOKIE_SECURE=1` and three cron entries —
`fetch-rates` daily, `check-limits` hourly, `sweep-uploads` weekly.

Two things are open deliberately.

The **account history page** (the list scoped to one account, with a card showing
its parent's rows) was left out because the account filter on the list already
answers the same question — build it when that stops being true.

**OCR on receipt photos** is not planned. Reading a total off a till slip is a
different project with a different failure mode: a number that is confidently
wrong is worse than no number, and the amount is typed before the camera opens.
