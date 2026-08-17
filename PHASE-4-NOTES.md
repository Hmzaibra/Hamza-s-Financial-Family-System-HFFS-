# Phase 4 — the month comparison, the export, the phone, and the machine

The last phase is four things that have almost nothing to do with each other,
and one thread running through all of them: **this is the phase where the app
stops being something you run and starts being something that runs.** A card
that reports on a month nobody asked to watch, a file that leaves the house, an
icon on a home screen, a service that comes back after a power cut — each of
them is a decision about what happens when nobody is looking at the screen.

---

## Part one — comparing a month to the one before it

### Opt-in, per account, and the reason is not performance

The obvious build is a comparison on every account summary. It is two more
queries on a screen that already runs several, on a Pi that can afford them.
Performance was never the argument.

The argument is that the comparison is only *interesting* on some accounts. A
current account that most spending goes through has a month worth watching:
groceries went up, eating out stopped, and both of those are facts about how the
household lived. A cash pocket topped up whenever someone passes an ATM has a
month that moves for reasons that are not about spending at all. Put a
comparison card on it and the household learns, within about three weeks, that
the card on that screen means nothing — and that lesson does not stay on that
screen. A number nobody can act on teaches people to skim past the numbers that
sit next to it.

So `007` adds one column, `accounts.reporting_enabled`, defaulted off, and the
account form grows a tick box. The household says which accounts it wants
watched.

The tick is hidden on a linked card, and that rule lives in two places for the
usual reason: `reference.py` forces the flag to `0` when the type is linkable,
whatever the form posted, and the template does not draw the box. A card's
figures are its parent's — that is what `settles_to()` means everywhere else in
this codebase — so a comparison on a card would report the parent's months under
the card's name. True, and the parent said twice.

What is *not* enforced is a database constraint on the pair. This is a
deliberate exception to the "new rules get enforced twice" convention, written
down here so it reads as a decision rather than an oversight: the cost of
getting it wrong is a duplicated card on one screen, and the cost of a CHECK is
that a household reorganising its accounts by hand in `sqlite3` hits a
constraint failure over a display preference. The comment in `007` says so.

### Both months come from the same function

`month_compare()` calls `month_here()` twice — once with a day in this month,
once with a day in last — rather than writing a second query with a wider date
range and splitting the rows. It is more SQL, executed twice, and it is the only
version where the figure on the comparison card and the figure on the month card
above it are the same number **by construction**.

The alternative fails quietly and late. Two queries that agree today drift the
first time one of them learns about something — settlement accounts, a new
direction, the transfer rule — and the symptom is two numbers a few pounds apart
on one screen, which is the single most expensive kind of bug this app can have.
Nobody reports it. They just stop believing the screen.

`month_here()` grew one thing to make this work: it already ranked categories
and returned the top five, and now it returns the full ranking as `by_category`
alongside. The comparison needs the categories that went **down** as much as the
ones that went up, and those are rarely in a top five.

### Up is bad, exactly here and nowhere else

Everywhere in this app, a positive number is money arriving and it is green. On
this card, spending £300 more than last month is the thing worth noticing, and
it is the alarming colour.

That inversion is the one thing about this feature most likely to be "fixed" by
someone later. So it is stated three times: in `month_compare()`'s docstring, in
the template comment above the class, and in `verify_phase4.py` as a check whose
label says what it is protecting. The classes are `.delta--worse` and
`.delta--better` rather than `--up` and `--down`, so the template reads as a
judgement rather than a direction — which is what it is.

Income on the same card keeps the ordinary direction: more coming in is better.
Two figures side by side with opposite colour logic sounds like a trap, but the
labels are "Spent" and "Came in", and nobody has ever thought that spending more
and earning more are the same kind of news.

### A category that stopped is a row

The card lists every category **either** month touched, sorted by the absolute
size of the change. A category that went to zero shows as `1,217.00 → 99.00`
with a "stopped" badge; one that did not exist last month is badged "new" and
carries no percentage, because there is nothing to divide by.

Listing only what was spent this month would have been half a card. The point is
what changed, and half of what changes in a household's month is something it
stopped doing. That is also usually the good news, and good news that the app
does not show is good news nobody notices.

Zero change is written as "same as July" rather than "— 0.00 · 0%", which is
three ways of saying nothing happened and takes longer to read than the
sentence.

---

## Part two — the export

### The file is the screen

`/transactions.csv` runs `_filters()` and `_where()` — the same two helpers the
list runs. Not a copy of them, the same functions, which is checked by a source
assertion rather than trusted.

That means the download carries whatever is on screen: the person filter, the
date range, the search box, and `visibility_sql()`. The last one matters most. A
download is a transaction read like any other, and it is not a way around
section 4: an admin exporting "everyone's" gets everyone's *shared* entries and
nobody's private ones, which is the same thing the screen shows them.

The one place the file deliberately differs is the row cap. The screen shows
fifty because nobody scrolls further; a file is opened precisely to look at
everything, and a household's lifetime of entries is a few thousand rows. So the
export has no limit, and the link says "Download all as CSV" so the difference
is visible before the download rather than after it.

### Amounts are text, and a cell is not a program

Every amount goes through `money.format_minor()` — the same helper every screen
uses — and lands in the file as `"200.00"`, not as a float. Invariant 1 does not
stop at the edge of the app. A CSV has no types; whatever is in the cell is what
the spreadsheet guesses, and creating a float in someone else's tool to be
mis-summed there is still this app's doing.

The other half is uglier. A cell beginning `=`, `+`, `-`, `@`, a tab or a
carriage return is a **formula** to Excel and LibreOffice, and
`=HYPERLINK("http://…"&A1)` typed into a merchant name is how a household's
ledger walks out of the house the next time the file is opened. Every cell that
a person could have typed into is prefixed with a quote when it starts with one
of those. The text stays readable; the spreadsheet stops executing it.

Merchant names and notes are the obvious vectors. The prefix is applied to every
column anyway, because "the obvious ones" is a judgement that ages badly and
the cost of being wrong about it is not recoverable.

### Export only

Import was considered and dropped. A CSV importer is not a parser, it is a
policy: what happens to a row whose account does not exist, whose currency is
not in the exponent table, whose amount has three decimal places in a
two-decimal currency, whose date is ambiguous between two conventions. Every one
of those has an answer, every answer is a rule that has to agree with
`_prepare()`, and `_prepare()` is the one place a transaction may be written
(invariant 5).

Nothing about the household's actual use needs it. Entries are typed at the
till, one at a time, on a phone. If a bank statement ever needs importing, that
is a project with its own phase and its own verification, not a flag on an
export.

---

## Part three — the phone

### Installable, and deliberately useless offline

`Add to Home Screen` gives the app its own icon, no address bar, and its own
entry in the app switcher. That needs three things: a manifest, an icon set, and
a service worker — Chrome will not offer the install prompt without one.

The service worker caches **nothing about money**. Two files and one page: the
offline page, the stylesheet, and an icon. Everything else goes to the network,
and when a *navigation* fails, the offline page is shown.

This is the whole argument, and it is the same one that runs through the rest of
the project: a stale balance looks exactly like a live one. Every screen in this
app is a query against a database that another person in the household is also
writing to. A cached figure was true at some point, is presented with the same
confidence as a true one, and there is no way for someone holding a phone to
tell which they are looking at. The dinosaur is a worse experience and an honest
one; the offline page is a better experience and an equally honest one.

For the same reason there is no Background Sync. Queuing a spend logged in a
tunnel and replaying it on landing sounds like a kindness until you follow it
through: the FX rate, the account balance and the card's daily limit are all
checked **at save time, on the server**, so a queued entry is one that has not
been checked yet and might be refused an hour later, in front of nobody. The
form keeps what was typed. Retry is a person pressing Save again.

The offline page says all of this in two sentences, because the person reading
it is standing somewhere with no signal wondering whether their coffee was
recorded.

### Small things that are only wrong once

**The worker is served from `/sw.js`, not `/static/js/sw.js`.** A service worker
may only control pages at or below its own URL, and `/static/js/` contains no
pages. The file still lives in `static/` with the rest of the JavaScript; a
three-line route serves it from the root.

**The manifest is served by a route too**, because `.webmanifest` is not in the
mimetypes table on a stock Windows or Debian box. Flask's static handler guesses,
Chrome refuses it, and the install prompt never appears with no error anywhere.

**The offline page does not extend `base.html`.** The worker stores it at install
time, cookie and all, so the layout's signed-in name would be frozen into a file
shown to whoever picks up the phone weeks later. It also has to render when the
server does not answer, so it depends on nothing: no context processor, no
session, no JavaScript, one stylesheet.

**The icons are drawn by a script.** `make_icons.py` builds all four from
rectangles — no font, so it runs on the Pi. The mark is geometry in the CSS and
geometry here, which means it regenerates exactly when the brand colour moves,
where a hand-edited PNG quietly diverges. Two details that are each a known way
to ship a bad icon: the maskable one is drawn *larger* inside a full-bleed
square, because Android crops it to the launcher's silhouette and an icon sized
for the plain tile ends up a stamp in a field of green; and the iOS one carries
no alpha channel, because iOS composites onto black and a transparent
`apple-touch-icon` is a black square on someone's home screen.

**No inline registration script.** The CSP has no `unsafe-inline` (invariant 8),
so `navigator.serviceWorker.register()` lives in `app.js`, after `load` — it
competes with first paint for the same connection otherwise, which is noticeable
over Tailscale to a Pi.

---

## Part four — the machine

Deployment is written down and not yet required. The laptop runs it now; the Pi
takes over later; `DEPLOY.md` covers both, and the app is identical on each.
What changes is who starts it and who runs the four jobs.

### Gunicorn's numbers are about SQLite, not about load

Two workers, four threads each. Four phones do not need more, but the reason for
the *shape* is that SQLite takes a write lock on the whole file: more processes
do not buy more write throughput, they buy more processes waiting on each other.
Two means a receipt being resized does not block the other person's page load,
and the threads soak up the reads.

`preload_app` is **off**, which is the one setting here that would cause real
damage if flipped for a plausible-sounding reason. Preloading forks after the
app is built, handing the same `sqlite3` connection to both workers — the
classic route to "database is locked", and to silent corruption on a bad day.

### The unit migrates before it serves

`ExecStartPre` runs `flask migrate`, so `git pull; systemctl restart expenses`
is the whole deployment and the 503 page is only ever seen by someone running
the dev server. It is idempotent — a database already at `007` prints nothing.

The hardening block is not theatre for a private network. The thing on that SD
card is a complete record of a household's money, sitting next to their photos.

### Four cron entries, and what each one costs when it stops

`fetch-rates` daily, `check-limits` hourly, `sweep-uploads` weekly,
`backup.py` nightly. The table in `DEPLOY.md` says what breaks in each case, and
one of those consequences is not obvious enough to leave in a table alone:

**`transactions.py` refuses a cross-currency transfer whose two sides disagree
by more than a factor of ten, and it does that by comparing against the cached
rate.** An empty cache has nothing to compare against, so the guard stays
silent. It is not a rule the app enforces; it is a rule the app enforces *if
cron is running*. That is written in `CLAUDE.md`, in `DEPLOY.md` and in the
crontab file itself.

Windows has no cron, so `deploy/windows-tasks.ps1` registers the same four with
Task Scheduler. Two differences that are worth knowing before something looks
broken: laptops are asleep at 04:00, so every task is registered
`StartWhenAvailable`; and the default is to stop on battery, which on a laptop
that lives unplugged means nothing ever runs.

### `tailscale serve`, not `funnel`

`serve` publishes to the tailnet with a real certificate — which is what makes
`https://` work on the phones with no warning and no port open on the router.
`funnel` publishes the same URL to the entire internet. One word apart, and
`DEPLOY.md` says so where someone typing the command will read it.

That certificate is why `SESSION_COOKIE_SECURE=1` belongs in `.env` on any
machine reached this way — and why the runbook's troubleshooting section leads
with the symptom it causes when it is set on a machine reached over plain http:
**login does nothing, with no error.** The cookie is set and never sent back.
The page just reloads.

---

## What phase 4 did not do

**No import.** See above — it is a policy, not a parser, and it would need its
own phase.

**No household-wide reporting screen.** The comparison is per account, which is
what was asked for and also the version that means something: a household-wide
month-over-month is dominated by whichever account had a big month, and the
answer to "why is this up" is always "look at the account", which is where the
card already is.

**No OCR**, still. A number that is confidently wrong is worse than no number,
and the amount is typed before the camera opens.

**No offline entry.** The whole of Part three.

---

## Verification

`verify_phase4.py` — 104 checks, in the house style. The two things it is
paranoid about:

The comparison's per-category rows must **sum to its own totals**, both months.
A card of small figures is unreadable by eye, and every one of them is
individually plausible; the only way to know it is right is arithmetic done a
second way.

The export must contain **exactly the rows the screen would show**, which is
checked by exporting as a member and looking for another person's private entry
in the bytes. It also checks that a merchant name that is a formula comes back
quoted, and that an ordinary cell does not.

Plus the flat facts that are only wrong once and never noticed: the manifest
parses, its icons exist and are the sizes it claims, its shortcuts point at URLs
that resolve, the worker is served from the root with `no-cache`, the offline
page needs no session and carries no name, the apple icon has no alpha, and each
of the four scheduled jobs appears in both the crontab and the PowerShell.

All nine suites pass: **787 checks**.

`shots_phase4.py` photographs the comparison card, an account without one, the
tick box, the list with the download link, and the offline page — 380px, light
and dark, as always. The card's zero-change wording was found in a screenshot,
with every assertion passing.
