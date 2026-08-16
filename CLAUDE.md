# Working on this codebase

Self-hosted household expense tracker. Flask + SQLite, no build step, no CDN, no
framework beyond Flask. Read `README.md` for the model and `PHASE-1-NOTES.md` for
why things are the way they are before proposing to change them.

## Commands

```bash
.venv/Scripts/python.exe -m flask --app app run --debug   # Windows dev server
.venv/bin/flask --app app run --debug                     # POSIX
```

Verification — run **all four** after any change; they build throwaway databases
and never touch `app.db`:

```bash
python verify_phase0.py && python verify_auth.py && python verify_phase1.py && python verify_accounts.py
```

270 checks at time of writing. A failing check is a real regression or a rule that
changed on purpose — if the latter, update the check *and* say so, never delete it.

Two of them are *source* checks rather than behavioural ones — no `date.today()`
in request-path code, and the stubbed `fx.fetch()` inversion. Both guard failures
that are invisible at runtime until the numbers are already wrong.

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
   fails closed with no session. Account balances are the one stated exception.
5. **Every transaction write goes through `transactions.create_transaction()`.**
   There is no second path. DB constraints are the backstop, not the guard.
6. **Every unsafe request carries a CSRF token.** No exemption list.
7. **Nothing hits the network during a request.** `fx.py` runs from cron only.
8. **No inline `style=` or `on*=` attributes.** The CSP has no `unsafe-inline`, so
   they silently do not render. User-supplied colours go in SVG `fill` attributes.
9. **Migrations are immutable once applied.** Add a new numbered file; never edit
   `001`–`004`. The runner reports a changed checksum rather than re-running.
10. **The entry form works with JavaScript off.** JS may only remove waiting.
    Anything it enforces must also be enforced server-side.

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

`transactions.py` is the write path and the entry form's queries. `reference.py`
is admin CRUD for accounts, categories and merchants. `fx.py` is the rate cache.
`visibility.py` is the section 4 rule, implemented once. `money.py` owns every
currency-shaped decision.

## Current state

Phase 1 is half done. The next chunk is the balance engine, and everything it
feeds — month totals, the transaction list, the account history page, live
overdraft prevention. `README.md` § *Where this goes next* lists what each piece
has to build on. Do not start Phase 2 (receipts) before that lands; the
`receiptless` flag and the `attachments` table have to agree with each other.
