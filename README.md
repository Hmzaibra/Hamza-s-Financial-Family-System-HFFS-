# Family Expense Tracker

Self-hosted household expense log. Flask + SQLite, no build step, no CDN.

**Phases 0 through 3 are complete.** The entry form, the account model,
balances, the month breakdown, the transaction list with filters, edit and
delete, receipt photos with the EXIF stripped off them, and budgets that warn
over Telegram are all built and verified. The family can log real purchases,
photograph the slip, and hear about it when a budget runs out.

See `PHASE-1-NOTES.md` and `PHASE-2-3-NOTES.md` for what was decided along the
way and why, and [Where this goes next](#where-this-goes-next) for Phase 4.

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

The CLI verbs:

| verb | what it does |
|---|---|
| `flask --app app run` | development server |
| `flask --app app migrate` | apply outstanding migrations |
| `flask --app app create-admin` | create the first admin, interactively |
| `flask --app app fetch-rates` | refresh cached exchange rates (from cron) |
| `flask --app app check-limits` | warn about budgets over their mark (from cron) |
| `flask --app app telegram-chats` | who has messaged the bot, with their chat ids |
| `flask --app app sweep-uploads` | unlink receipt files whose rows are gone |

`create-admin` is only needed once. After that, people are added in
Setup → People, which is also where a Telegram chat id is pasted.

Verify the install:

```bash
python verify_phase0.py     # schema constraints, money, visibility
python verify_auth.py       # login, lockout, cookies, headers
python verify_phase1.py     # entry form, merchant defaults, transfers, undo, CSRF
python verify_accounts.py   # account links, cards, cash rules, limits, rates
python verify_balances.py   # balance arithmetic, month figures, edit and delete
python verify_receipts.py   # EXIF stripping, resizing, who may see a photo, orphans
python verify_limits.py     # period maths, scopes, who is told, and how often
```

492 checks. They build their own throwaway databases and touch nothing in
`app.db` or `uploads/`.

## Layout

| path | what it is |
|---|---|
| `app.py` | app factory, security headers, and the five CLI commands |
| `config.py` | env-driven config; no hostnames, no TLS logic |
| `db.py` | connections, per-connection pragmas, `today_for()`, `migrate` command |
| `migrate.py` | numbered `.sql` runner (not Alembic) |
| `money.py` | minor-unit parsing and formatting. The only place `Decimal` appears |
| `visibility.py` | the section 4 rule, implemented once |
| `csrf.py` | session-bound CSRF token, applied to every unsafe method |
| `transactions.py` | the single validated write path, plus the entry form's queries |
| `fx.py` | the exchange-rate cache. One of two files that reach the internet |
| `telegram.py` | Bot API sending. The other one, and cron-only like the first |
| `balances.py` | balance arithmetic and the month figures. A stated exception to rule 4 |
| `limits.py` | budgets: period maths, scope maths, and the alert sweep. The other exception |
| `receipts.py` | the image pipeline — resize, thumbnail, strip EXIF — and orphan cleanup |
| `migrations/` | `001` schema, `002` seed, `003` accounts, `004` receipts + rates, `005` photos + budgets |
| `blueprints/` | `auth.py`, `entry.py` (section 6), `ledger.py` (list, edit, delete), `receipts.py` (serve, attach, remove), `reference.py` (setup), `dashboard.py` |
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
   session. There are exactly two deliberate exceptions, account balances and
   budget figures, and each says so in its own module docstring — a filtered
   aggregate over the household is a *wrong* number rather than a partial one.
5. **Parameterised SQL only.** The single f-string into a query is
   `visibility_sql()`'s own literal fragment; its params stay bound.
6. **Every transaction write goes through `transactions._prepare()`.** Creating
   and editing both call it, so an edit cannot pass a check a new entry would
   fail — and the period sums (a card's daily withdrawal ceiling, a credit
   card's monthly limit) exclude the row being replaced, or raising 500 to 600
   would read as 1,100 against the day. The database CHECK constraints and
   triggers are the backstop, not the only guard.
7. **Every unsafe request carries a CSRF token.** No exemption list.
8. **Nothing is fetched from the internet during a request.** Two commands
   touch the network, `fetch-rates` and `check-limits`, and both run from cron.
   `verify_limits.py` checks by reading the source that no blueprint imports
   `telegram`, because that is the kind of rule that decays quietly.
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

## Balances

A balance is `opening_balance_minor` plus every leg that touched the account,
computed on read rather than stored. There is no running total to drift, and
nothing to rebuild when an entry is edited or deleted.

Two things about it are worth knowing:

- **A card's balance is its parent's.** A debit card and an Instapay handle are
  ways of reaching a bank or wallet, so every leg resolves to its *settlement
  account* before anything is summed, and the card row reports the figure it
  actually draws on, marked `via <account>`. That is the whole meaning of the
  link.
- **Balances skip `visibility_sql()`** — rule 4's stated exception. A member
  looking at a shared account would otherwise see a balance computed from a
  subset of its rows, which is not a partial answer but a wrong one. Aggregate
  totals leak; individual transactions never do.

Mixed currency is handled where it can be and declined where it cannot. A
foreign charge on a base-currency account converts through the rate captured at
entry. A base-currency charge on a foreign-currency account has no rate pointing
the right way, so it is counted and the balance is marked *approx* rather than
guessed at — the same call the international card limit makes.

Below zero on anything but a credit card is **coloured, never blocked**. A
negative balance is far more often a missing opening balance or an unlogged
income than a purchase that should not have happened, and refusing the save
would punish the person for the ledger being behind.

## Receipts

A photo taken at the till posts with the entry it belongs to — the camera sits
in `.pos__aux`, the zero-width grid column Phase 0 reserved for it, as a `<label>`
wrapping a hidden file input. That construction is the only one that opens the
camera with JavaScript switched off; a `<button>` would need a click handler.

Nothing arrives on disk as it was sent. Every upload is decoded by Pillow,
re-encoded, and written under a UUID name this app chose:

- **EXIF is stripped**, which is the point rather than a side effect. A phone
  writes the GPS coordinates of wherever the photo was taken, plus the device
  model and the capture time. A pharmacy receipt is private enough on its own;
  the same file annotated with the pharmacy's location is a different thing, and
  nobody — including whoever ends up with a copy of the backup — has a use for
  it. The pixels are pasted into a blank image rather than filtered through a
  blocklist of tags, because a blocklist is a list somebody has to keep current.
- **Orientation is applied first.** It is the one tag that changes what you see,
  so it is spent on the pixels before the rest is discarded. Skip that and every
  receipt shot in portrait is stored on its side, permanently.
- **The long edge is capped at 1600px** and a 320px thumbnail is made. A 4MB
  camera original becomes about 300KB, which on a Pi serving a phone over
  Tailscale is most of the experience.
- **PNG stays PNG**, everything else becomes JPEG. A screenshot of a bank
  transfer confirmation is a real receipt in this house, and JPEG turns its text
  to mush. HEIC is not handled: decoding it needs a second compiled dependency,
  Safari already converts on upload, and the error message says so in plain
  words if it ever does not.

`uploads/` sits outside `static/`, so a photo is only reachable through
`blueprints/receipts.py`, which loads the attachment's transaction and applies
the section 4 rule to it. A UUID filename is not a permission model — it is
unguessable right up until the first time someone forwards a link.

### A photo and "there was no receipt"

They cannot both be true, and the two ways of reaching that contradiction are
handled differently on purpose:

| what happens | what the app does |
|---|---|
| you attach a photo to an entry marked receiptless | the flag is cleared |
| you tick receiptless on an entry that has photos | refused, with a sentence |

A photo is evidence and the flag is a claim, so the evidence wins. Migration
`005` enforces both directions in triggers as well, and the edit screen hides
the tick box entirely once photos exist rather than offering a control that will
be refused the moment it is used.

### Files that outlive their rows

`ON DELETE CASCADE` removes the attachment row. It does not remove the JPEG.
Unlinking from Python straight after the DELETE works until the process dies in
between — and then the picture is on disk with nothing pointing at it, invisible
and permanent. So an `AFTER DELETE` trigger records the debt in `orphaned_files`
inside the same transaction that creates it, and `receipts.reap()` pays it off:
after a delete in the request, and again from `flask sweep-uploads` for anything
a crash left behind. A crash costs a delayed unlink, never a leak.

That trigger only fires on a cascade because `db.py` sets
`PRAGMA recursive_triggers = ON`. SQLite's foreign-key actions do not fire
triggers otherwise — off by default, silent when absent, and the uploads folder
would simply grow forever.

## Budgets

Budgets **warn and never block**. An app that refuses to record a purchase
because the purchase was unwise has stopped being a record of what happened.
They are unrelated to `credit_limit_local_minor`, `credit_limit_intl_minor` and
`withdrawal_limit_minor` on `accounts` — those are ceilings a bank set, they are
real, and `transactions.py` does refuse a save that would cross one.

A budget is a name, an amount, a period, and what it is about: the household,
one person, one category, one account or one merchant. A parent category counts
its children, and an account counts the cards and Instapay handle that draw on
it — the same settlement rule its balance follows. Periods are calendar months
or ISO weeks (Monday start), so the edges are ones a phone calendar agrees with.

Budgets are denominated in the base currency, enforced by a trigger, because the
spending they are compared against is totalled in the base currency and a
ceiling in another unit is not a comparison. If the household's base currency
ever changes, existing budgets are *skipped* by the sweep with a line saying so
rather than silently converted.

### Who sees which budget

This is the second stated exception to rule 4, and it is stated rather than
inherited from the first. The *figures* inside a budget read across the whole
household, filtered by nothing, exactly as balances do: a household budget
filtered to what the reader personally may see is not a partial answer, it is a
number that says the family has 3,000 left when it has 200.

What section 4 filters is *which budgets a person sees at all*. A budget about
one family member belongs to that member and to admin. Household, category,
account and merchant budgets are shared facts and everyone sees them. A member
should not learn from a progress bar that someone else is 90% through their
personal allowance.

### Telegram

```bash
flask --app app check-limits --dry-run   # decides everything, sends nothing
flask --app app check-limits             # for cron, hourly is fine
flask --app app telegram-chats           # who has messaged the bot, with ids
```

A cron command and never a request, for the reason invariant 8 exists: a budget
warning is the least urgent thing in this app and must never sit between someone
and their entry form.

Two messages per budget per period at most — one at the mark you set, one when
it is all spent — guaranteed by `limit_alerts`' UNIQUE on
`(limit_id, period_key, threshold_pct)`, which is why the command is safe to run
every hour. **Nothing is recorded unless a message actually left.** Recording
first and failing to send would mean a warning permanently owed and never
delivered; this way a flat network or a blocked bot costs a retry next run.

Setting it up is three steps and the middle one is not optional: put the token
from @BotFather in `.env`, have each person send the bot any message, then run
`telegram-chats` and paste the numbers into Setup → People. Telegram will not
let a bot write to someone who has never written to it. That is a spam rule.

## Where this goes next

- **Phase 4 — reporting, PWA, CSV, deployment.** `fx_rates` and the
  per-transaction captured rate are what multi-currency reporting needs.
  Deployment is gunicorn behind `tailscale serve`; set `SESSION_COOKIE_SECURE=1`
  when TLS is in front, or the session cookie is set and never sent back and
  login silently fails. Two cron entries come with it — `fetch-rates` daily and
  `check-limits` hourly — plus `sweep-uploads` weekly.

Left out on purpose: an **account history page**. The list's account filter
already answers "what moved through this account", and a second screen showing
the same rows is a second thing to keep correct. Build it when the filter stops
being enough.

Also left out: **OCR on receipt photos**. Reading a total off a till slip is a
different project with a different failure mode — a number that is confidently
wrong is worse than no number, and the amount is already typed before the camera
is opened.

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
