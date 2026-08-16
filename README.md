# Family Expense Tracker

Self-hosted household expense log. Flask + SQLite, no build step, no CDN.

**Phase 0 is complete.** **Phase 1 is half done**: the entry form, the account
model and the reference data behind them are built and verified; the transaction
list, filters, edit/delete, account balances and the monthly summary are next.
See `PHASE-1-NOTES.md` for what was decided along the way, and
[Where this goes next](#where-this-goes-next) for what is left and what it has to
build on.

## Running it

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
python -c "import secrets; print(secrets.token_hex(32))"   # paste into SECRET_KEY

export FLASK_APP=app
flask migrate                                              # creates app.db
flask create-admin                                         # prompts for a password
flask run                                                  # http://localhost:5000
```

On Windows the venv lives at `.venv\Scripts\` instead of `.venv/bin/`, and the
commands are the same with `.venv/Scripts/python.exe -m flask --app app <verb>`.

The four CLI verbs:

| verb | what it does |
|---|---|
| `flask --app app run` | development server |
| `flask --app app migrate` | apply outstanding migrations |
| `flask --app app create-admin` | create a user, interactively |
| `flask --app app fetch-rates` | refresh cached exchange rates (from cron) |

Verify the install:

```bash
python verify_phase0.py     # schema constraints, money, visibility
python verify_auth.py       # login, lockout, cookies, headers
python verify_phase1.py     # entry form, merchant defaults, transfers, undo, CSRF
python verify_accounts.py   # account links, cards, cash rules, limits, rates
```

They build their own throwaway databases and touch nothing in `app.db`.

## Layout

| path | what it is |
|---|---|
| `app.py` | app factory, security headers, `create-admin`, `fetch-rates` |
| `config.py` | env-driven config; no hostnames, no TLS logic |
| `db.py` | connections, per-connection pragmas, `today_for()`, `migrate` command |
| `migrate.py` | numbered `.sql` runner (not Alembic) |
| `money.py` | minor-unit parsing and formatting. The only place `Decimal` appears |
| `visibility.py` | the section 4 rule, implemented once |
| `csrf.py` | session-bound CSRF token, applied to every unsafe method |
| `transactions.py` | the single validated write path, plus the entry form's queries |
| `fx.py` | the exchange-rate cache. The only code that reaches the internet |
| `migrations/` | `001` schema, `002` seed, `003` accounts, `004` receipts + rates |
| `blueprints/` | `auth.py`, `entry.py` (section 6), `reference.py` (setup), `dashboard.py` |
| `templates/`, `static/` | Jinja, plain CSS, self-hosted Inter |
| `scripts/backup.py` | SQLite backup API + `uploads/` tarball |

Everything the app owns lives in this folder. Moving to the Pi is `rsync` plus a
systemd unit.

## Rules the code assumes

1. **Money is integer minor units** in `*_minor` columns. `Decimal` appears only
   in `money.parse_to_minor()`. `money.py` refuses a `float` argument outright,
   and no function in it returns one. Conversion via `fx_rate_to_base` happens in
   Python — never as `SUM(amount_minor * fx_rate_to_base)` in SQL, which is float
   arithmetic on money through the back door.
2. **Amounts are positive.** Direction carries the sign. The DB enforces it.
   Balances follow the same idea: only a credit card may sit below zero.
3. **Timestamps UTC** (`YYYY-MM-DDTHH:MM:SSZ`), **dates are local calendar dates**
   — local to the *user's own* timezone, via `db.today_for(user['timezone'])`.
4. **Every transaction query goes through `visibility_sql()`.** It returns a SQL
   fragment plus params so aggregates compose it too, and fails closed with no
   session. Account balances are the one deliberate exception (see below).
5. **Parameterised SQL only.** The single f-string into a query is
   `visibility_sql()`'s own literal fragment; its params stay bound.
6. **Every transaction write goes through `transactions.create_transaction()`.**
   There is no second path. The database CHECK constraints and triggers are the
   backstop, not the only guard.
7. **Every unsafe request carries a CSRF token.** No exemption list.
8. **Nothing is fetched from the internet during a request.** `fetch-rates` runs
   from cron and writes to a cache; see [Exchange rates](#exchange-rates).
9. **No inline `style` or `on*` attributes anywhere.** The CSP has no
   `unsafe-inline` and never will, so an inline style is not "slightly wrong" —
   it silently does not render. Colours a user supplies are painted as SVG
   `fill` attributes instead.

## The account model

Six account types, and the shape of the relationships between them is the point:

- **bank** and **wallet** hold money, and can hold links.
- **debit_card** and **instapay** are *ways of reaching* a bank or wallet, not
  second pots. They carry `parent_account_id`, one level deep, and take their
  parent's currency. An account may have **one Instapay handle** and **as many
  debit cards as it likes**.
- **credit_card** is its own thing — the bank's money, not yours — so it takes no
  link, and it is the only type allowed to open or sit in debt.
- **cash** is linked to nothing.

Cards carry a **network**, a **colour**, an **expiry month** and a **daily cash
withdrawal ceiling**; credit cards additionally carry a **monthly local limit**
and a **monthly international limit**, because Egyptian banks routinely set those
to different numbers. A card whose expiry month has passed deactivates itself the
next time the accounts list or the entry form is read — both against the
reader's own calendar day, never the server's, or the two screens disagree for
up to a day.

An Instapay handle is stored as `name` = `"Sam - @sam_pay"`, with the
handle also in its own column so the edit form can show the two halves apart.
Neither half identifies the account on its own.

Cash is deliberately narrow: it records **spending, income and reimbursements**,
and nothing transfers out of it. Money *into* cash is a withdrawal, and a
withdrawal happens on a card — so a transfer into a cash account from a bank,
wallet or Instapay is refused with the linked cards named. This is the one place
the app argues with the person using it, and it does so because "the bank gave me
cash" loses the fact the statement will actually show.

Every rule above is enforced in `blueprints/reference.py` for a sentence the user
can act on, and again by triggers in `003`/`004` so that nothing else can write
around it.

## What a transaction records

`direction` is `spend`, `income` or `transfer`, and it carries the sign.
Reimbursements are `income` filed under the seeded **Reimbursement** category,
rather than a fourth direction — mechanically they are identical, and adding a
direction means rebuilding the `transactions` table to change a CHECK.

Two facts that look related and are not:

- **`merchant_id`** — who was paid. NULL is a real answer, not a gap.
- **`receiptless`** — whether there is a paper trail.

A nameless stall may print a slip; a named shop may not. Until `004` these were
welded together as a seeded "Receipt-less" merchant, which forced a choice
between recording who you paid and recording whether you can prove it. They are
separate columns now and the entry form offers separate controls.

Merchants belong to **spending** or **income**, never silently to both:
`merchants.kind`. Who you buy from and who pays you are different lists that
happen to share a table, and mixing them puts an employer in the chip row while
you are standing at a till.

## Exchange rates

`fx.py` and the `fx_rates` table are a **cache**, never a source of truth. What
is stored against a transaction is the rate that was in the box at save time,
because the rate on the day of purchase is the only correct one and it is
unrecoverable afterwards. The cache only means the box arrives with a plausible
number in it.

```bash
flask --app app fetch-rates          # refresh if the cache is over a week old
flask --app app fetch-rates --force  # refresh regardless
```

Run it **daily** from cron. Without `--force` it does nothing until the cache is
older than `FX_MAX_AGE_DAYS` (default 7), so a daily schedule produces weekly
rates, survives the Pi being asleep on Tuesday, and never hammers a provider.

```cron
0 4 * * *  cd /home/pi/expenses && .venv/bin/flask --app app fetch-rates
```

The provider is configurable (`FX_RATES_URL`), free, and needs no key. Providers
quote "1 EGP = 0.0172 EUR"; a transaction stores the other direction, so `fx.py`
inverts on the way in — getting that backwards is a silent 3000× error.
`verify_accounts.py` asserts it twice: that the cache reads back foreign → base,
and that `fetch()` itself inverts, with the provider stubbed out so the check
costs no network. The second is the one that matters — the cache round-trip
would keep passing if the inversion were removed.

A failed fetch is logged and shrugged off. The cache keeps its old numbers, the
entry form still works, and it works with no cache at all.

### What "works with the internet unplugged" means

No third-party runtime dependencies. The font is a file in `static/fonts/`, not a
Google Fonts link; there is no CDN script, no analytics, no remote stylesheet;
and `default-src 'self'` forbids the browser from fetching anything the app did
not serve. Your ISP, a CDN outage or an API changing its pricing can never break
the entry form.

It does **not** mean offline entry. There is no service worker and no outbox: if
the phone cannot reach the Pi, nothing is logged. Queued offline entry is the
honest reading of the `PWA` item in Phase 4, and the hard part is not storage —
it is that inline merchant creation, the CSRF token and the ten-minute undo
window all assume a live round trip.

## Decisions taken in Phase 0

- **Admin is created by CLI**, not seeded in a migration — a migration with a
  password hash is a committed credential.
- **Per-user timezone** (`users.timezone`, default `Europe/Berlin`). A 1am Cairo
  purchase files under the Cairo day even when the server is in Aachen.
- **Ten tables, not nine.** `login_attempts` backs login rate limiting; an
  in-process counter is wrong as soon as gunicorn runs more than one worker.
  (Eleven now — `fx_rates` arrived in `004`.) Counting the spec's nine domain
  tables plus those two; `.tables` shows twelve because `schema_migrations` is
  the runner's own bookkeeping and was never one of the nine.
- **`counter_amount_minor` / `counter_currency`** on transactions, so a EUR→EGP
  transfer records both sides. Always populated on transfers, even same-currency
  ones, so balance maths never branches on NULL.
- **Account balances are computed unfiltered**, deliberately bypassing rule 4: a
  member's balance on a shared account would otherwise be wrong rather than
  merely partial. Aggregate totals leak; individual transactions do not.
- **`--muted` darkened** from the spec's `#6B7C78` to `#62736F`. The spec value
  measures 4.39:1 on the card, under AA for text at this size.
- **Dark-mode accent lifted** from `#1F6F63` to `#4FA595`. The spec pine measures
  2.70:1 on the dark card; the lifted value is 5.49:1.
- Migrations are immutable once applied. The runner reports a changed file rather
  than silently re-running it — add a new number instead of editing an old one.

## Decisions taken in Phase 1

- **The entry form owns `/`**; the dashboard moved to `/dashboard`. This screen
  is the product.
- **CSRF tokens on every unsafe method**, no dependency, no exemption list.
- **Links are a parent pointer, one level deep**, with the legality rules as
  triggers rather than form validation, because the form is not the only thing
  that will ever write.
- **Card limits are periods, not transactions.** The withdrawal ceiling is a
  calendar day and the credit limits are a calendar month, so both are enforced
  by summing the period. A charge in the card's own currency is local and
  anything else is international — the only honest split available without a
  merchant country, and it is stated here because it is a judgement call.
- **The international limit is not enforced on a card denominated in something
  other than the base currency.** Each transaction carries its rate *to base*, so
  summing foreign charges lands in base, and comparing that to a ceiling set in
  another unit is arithmetic on two different things. It declines rather than
  guesses.
- **Undo is owner-only and expires after ten minutes.** Admin is deliberately not
  special: wider would make it a deletion tool wearing a friendlier label.
- **Nothing hard-deletes.** Accounts and categories carry transactions; a
  merchant you stop using should leave the chips without rewriting last March.
  The one exception is the retired Receipt-less merchant in `004`, whose meaning
  moved into a column rather than being lost.
- **Signing out asks first**, as a page rather than a `confirm()` — the CSP
  forbids inline handlers, and the question then survives with scripting off.

## Where this goes next

The remainder of **Phase 1** is one coherent chunk, and the balance engine is its
spine:

| what | what it builds on |
|---|---|
| Account balances | `opening_balance_minor`, plus every transaction's `amount_minor` and the transfer's `counter_amount_minor`. Deliberately unfiltered (rule 4's stated exception). An **instapay account's balance is its parent's** — that is the whole meaning of the link, and it is the one part of "changes to Instapay also happen to the bank" that is not yet visible anywhere. |
| Month totals on `/dashboard` | `visibility_sql()` composed into a `SUM`, converting through `money.convert_to_base()` in Python, never in SQL. The template already has the shape; only the `0.00` is a placeholder. |
| Transaction list, filters, edit, delete | `visibility_sql()` for reads; edits must go back through a validated write path, not a second `UPDATE`. `ix_txn_vis` exists for exactly this query shape. |
| Account history page | The balance engine plus the list, scoped to one account, with an instapay handle showing its parent's rows. |
| Overdraft prevention | Once balances exist, rule 2's balance half can be enforced live: refuse a spend that would take a non-credit-card account below zero. Not built now because with no opening balances entered it would block the very first entry. |

Then:

- **Phase 2 — receipts.** `attachments` is already in the schema with an
  `ON DELETE CASCADE`; `MAX_CONTENT_LENGTH` is already set; the entry form's
  `.pos__aux` grid column is a zero-width seat for the camera button so it drops
  in without reflow. `transactions.receiptless` is the flag that says not to
  expect one — the two should agree, and a receiptless transaction with an
  attachment is a contradiction worth surfacing.
- **Phase 3 — limits and Telegram.** The `limits` and `limit_alerts` tables are
  built and unused; `limit_alerts`' UNIQUE constraint is what stops a threshold
  nagging twice. Note these are *budgets*, unrelated to the card limits on
  `accounts`, which are ceilings the bank set. `users.telegram_chat_id` and
  `TELEGRAM_BOT_TOKEN` are both already in place.
- **Phase 4 — reporting, PWA, CSV, deployment.** `fx_rates` and the per-transaction
  captured rate are what multi-currency reporting needs. Deployment is gunicorn
  behind `tailscale serve`; set `SESSION_COOKIE_SECURE=1` when TLS is in front,
  or the session cookie is set and never sent back and login silently fails.

Known rough edge to fix whenever it next annoys someone: **with JavaScript off,
both merchant lists render at once** under their own headings. Hiding the wrong
one server-side would make income sources unreachable without a page reload,
since nothing re-renders on a direction change. The server refuses a merchant
picked from the wrong list, so it cannot produce bad data — it is only noisier.

## Backups

```bash
python scripts/backup.py --keep 14
```

Uses SQLite's online backup API, not `cp`: copying a live database in WAL mode
misses whatever is still in the `-wal` file, and the copy restores looking fine
while missing the last few transactions. Restore steps are in the script's
docstring.
