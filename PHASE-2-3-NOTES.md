# Phase 2 and 3 — receipts, budgets, and the two things that had to be said out loud

Phase 1 answered "where did the money go". These two phases answer "can I prove
it" and "was that more than we meant to spend". They shipped together because
they turned out to share a problem: both of them wanted to read the household's
spending without a reader, and only one of them had permission.

---

## Part one — receipts

### The contradiction was the design question

Migration `004` had already split "there was no receipt" out of the merchant
list and made it a property of the transaction. That was the right move, and it
created a state the schema could now express and the app had to have an opinion
about: a transaction marked `receiptless` with a photo attached to it.

Three options were on the table.

**Let both be true and show both.** Cheapest, and wrong — the list would print
"no receipt · 1 photo" on the same row, and a person reading that has learned
nothing except that the app is not paying attention.

**Refuse both directions symmetrically.** Consistent, and annoying in exactly
the case that matters: you tick "no receipt" at the till because the vendor is
a stall, then he prints a slip after all, and now the app makes you go back and
untick something before it will take the photograph.

**Asymmetric, which is what shipped.** Attaching a photo clears the flag;
ticking the flag on an entry that has photos is refused. The two acts carry
different amounts of information. A photo is evidence and settles the question.
A tick is a claim, and a claim that contradicts evidence already in hand is the
one worth stopping.

Both directions are enforced twice, per the house rule: `receipts.store()`
clears the flag in the same database transaction as the insert, `_prepare()`
raises an `EntryError` with a sentence, and migration `005` has a trigger for
each so nothing can write around either.

One consequence worth noticing on screen: the edit page **hides** the
receiptless tick box once photos exist, and says why, rather than leaving a
control that would be refused the moment you used it.

### EXIF, and why it is a strip rather than a filter

A phone camera writes GPS coordinates into every photo. Also the device model,
and the capture time to the second.

A receipt for a pharmacy is a fairly private thing already. The same file
annotated with the pharmacy's exact location is a different thing, and it is a
thing that ends up in the backup tarball, on whatever laptop the backup is
restored to, and in any copy that ever gets shared. Nobody in this household has
a use for it.

The implementation is a `Image.new()` plus `paste()` — the pixels are copied
into a blank image and the metadata is simply left behind. The alternative,
deleting known-bad tags, is a blocklist, and a blocklist is a list somebody has
to keep current against a format that keeps growing. This way there is nothing
to keep current: the saved file has never had metadata in it, rather than having
had it removed.

Orientation is the exception and is spent before the strip. It is the one tag
that changes what you see, and dropping it unapplied means every receipt shot in
portrait is stored on its side, permanently, with no way back. `verify_receipts.py`
checks this with a real 400×200 image tagged "rotate 90" and asserts the stored
file is taller than it is wide.

The GPS check is deliberately paranoid: it writes real coordinates into a real
JPEG, asserts the *source* has them (so the check cannot pass by testing
nothing), runs the real pipeline, and reads the bytes back off disk looking for
both the parsed tag and the raw `Exif\x00\x00` marker.

### Files that outlive their rows

`attachments` has had `ON DELETE CASCADE` since `001`. That removes the row. It
does not remove the JPEG, and the JPEG is the part that takes up an SD card.

The obvious fix — unlink from Python right after the DELETE — works until the
process dies in between. Then the file is on disk with nothing pointing at it:
invisible, unreferenced, and there forever. There is no query that finds it
later, because the thing that knew about it is gone.

So the debt is recorded in the same transaction that creates it. An
`AFTER DELETE ON attachments` trigger writes both paths into `orphaned_files`,
and `receipts.reap()` pays it off — immediately after a delete in the request,
and again from `flask sweep-uploads` for anything a crash left behind. A crash
costs a delayed unlink. It never costs a leak.

**This is where the pragma comes in.** SQLite does not fire delete triggers for
foreign-key cascades unless `recursive_triggers` is on, and it is off by
default. Without it the trigger works perfectly for the case nobody cares about
(deleting one photo) and silently never fires for the case it exists for
(deleting a transaction that has photos). `db.py` sets it next to
`foreign_keys`, with a comment, and `verify_receipts.py` checks both that our
connections have it on *and* that a plain `sqlite3.connect()` has it off — so
the check documents why the line is there rather than just asserting it is.

### Where the camera sits

Phase 0 left `.pos__aux` as a zero-width third grid column on the till display,
with `grid-template-columns: auto 1fr var(--pos-aux, 0px)`. Phase 2 filling it
was setting `--pos-aux: 46px`. Nothing else on that screen moved.

It is a `<label>` wrapping a visually-hidden `<input type="file">`, not a
button. That is the only construction that opens the camera with JavaScript
switched off — a `<button>` would need a click handler to reach the input, and
invariant 10 says the entry form works without scripting. `capture="environment"`
asks a phone for the back camera and is ignored by a desktop browser, which is
the right behaviour in both places.

The photo posts with the entry rather than after it. The transaction does not
exist yet when the photo is taken, and "save, wait for the redirect, find the
row, attach" is a round trip in the middle of the one screen built to have none.
The form is `multipart/form-data`, which costs nothing when no file is chosen.

A photo that will not decode never costs you the purchase: the entry is already
saved by then, and the failure is a flash message, not a rollback.

---

## Part two — budgets

### They warn. They do not block.

The spec said so and it is worth restating, because the app already has code
that *does* refuse a save over a limit — `_check_credit_limit()` and
`_check_cash_withdrawal()`. Those are ceilings a bank set. They are facts about
the world: the card will be declined, so recording the purchase would be
recording something that did not happen.

A budget is an intention. An app that refuses to record a purchase because the
purchase was unwise has stopped being a record of what happened, which is the
only job it has. Nothing in `limits.py` raises at a writer; every function
returns numbers.

The two ideas share an English word and nothing else, so the settings screens
say which is which in as many words.

### The visibility exception, stated

This is the part that needed a decision rather than an implementation.

Rule 4 says every transaction read goes through `visibility_sql()`, and named
`balances.py` as *the one* stated exception. Budgets need the same exemption for
the same reason, and the honest move was to change the rule rather than quietly
claim balances' excuse.

A household budget filtered to what the reader personally may see is not a
partial answer. It is a wrong one — a number that says the family has 3,000 EGP
left when it has 200. Aggregates over the household are the household's own
business; individual transactions never are.

And the sweep has no reader at all. `visibility_sql(None)` fails closed, which
is exactly right for a request and useless for cron: every budget would read as
zero spent, forever, silently, and nobody would ever get a warning.

So the arithmetic is unfiltered, and CLAUDE.md's invariant 4 now says "exactly
two exceptions" with both named.

What section 4 *does* filter is which budgets a person may see at all, in
`visible_limits()`. A budget about one family member belongs to that member and
to admin. Household, category, account and merchant budgets are shared facts and
everyone sees them. A member should not learn from a progress bar that someone
else is 90% through their personal allowance — that is an individual fact
wearing an aggregate's clothes.

`verify_limits.py` checks both halves: that a member cannot see another person's
budget, and that the figure inside a budget they *can* see is byte-identical to
what admin sees.

### Scope, and the shape of the form

A budget can be about the household, one person, one category, one account or
one merchant. Two of those inherit rules from elsewhere in the app:

- **A parent category counts its children.** Same reasoning as the ledger's
  category filter: otherwise organising your categories quietly shrinks your
  budgets.
- **An account counts the cards and Instapay handle that draw on it.** The
  settlement rule `balances.py` resolves before it adds anything up. A budget on
  "CIB" that ignored the CIB debit card would be measuring a fiction.

The form encodes scope as **one** `<select>` whose values look like
`category:7`. The obvious design — a "kind" dropdown plus four "which one"
dropdowns, three of them hidden — cannot work with JavaScript off, because there
would be no way to hide the three. One grouped select says the same thing in one
tap and cannot be filled in inconsistently.

Budgets are denominated in the base currency, enforced by a trigger, for the
same reason `_check_credit_limit()` declines to check an international limit on
a non-base card: comparing a ceiling in one unit against a total in another is
not a comparison. If the household's base currency ever changes, existing
budgets are *skipped* by the sweep with a line explaining why, rather than
converted behind someone's back.

### Alerts: the ordering that matters

Two messages per budget per period at most — one at the mark you set, one when
it is all spent. `limit_alerts`' UNIQUE on
`(limit_id, period_key, threshold_pct)` is what makes that a guarantee rather
than an intention, and it is why `check-limits` is safe to run hourly from cron.

The non-obvious decision is **when the row is written**. Recording the alert and
then sending it means a failed send is a warning permanently owed and never
delivered — the UNIQUE constraint would keep it quiet forever. So nothing is
recorded unless a message actually left. A flat network, a bad token, or a
family member who blocked the bot costs a retry on the next run, and the failure
is a line in the cron log rather than silence.

The same logic covers the case where nobody has a chat id yet: nothing is sent,
nothing is recorded, and the moment someone pastes an id into Setup → People the
next run delivers what was owed.

Recipients follow the budget's subject: a budget about one person messages that
person *and* admin, because admin set it and will be asked about it. Everything
else goes to admin only — a category budget is not news anyone else asked for.

`--dry-run` exists because setting up a cron entry is exactly when you want to
see the whole decision without any of the consequences.

### Setup → People, which Phase 3 forced

Telegram alerts go to a chat id, and until now the only way to put one on a user
was `sqlite3` over SSH. `flask create-admin` was also the only way this household
could gain a second member.

So Phase 3 includes a people screen: add, edit, set a password, set a timezone,
set a chat id, switch someone off. Two rules in it are worth naming:

- **The username is not editable.** It is what `login_attempts` records against
  and what a phone's password manager has already saved. Renaming buys nothing
  and breaks both.
- **The last active admin cannot demote or deactivate themselves.** The cure for
  getting that wrong is `sqlite3` on the Pi, which is precisely the thing this
  screen exists to avoid.

Telegram's own rule shapes the setup flow and there is no way around it: a bot
cannot message someone who has never messaged it. So the sequence is token in
`.env`, each person sends the bot anything, `flask telegram-chats` prints the
ids, paste them in. The `.env.example` comment spells this out, because the
first time it fails it looks like a bug in this app.

---

## What was rejected

**OCR on the receipt photos.** Reading a total off a till slip is a different
project with a different failure mode, and a number that is confidently wrong is
worse than no number. The amount is already typed before the camera opens.

**HEIC support.** iPhones shoot it by default, but Safari converts to JPEG when
a photo is attached to a form, and decoding it otherwise means `pillow-heif` —
a second compiled dependency on a Pi to solve a problem that mostly does not
occur. What shipped instead is an error message that names HEIC by name, so if
it ever does occur the person reading it knows what happened.

**Deleting budgets.** They archive with `is_active = 0` like everything else,
even though nothing references them and a hard delete would be safe. Consistency
across the Setup screens is worth more than saving one row.

**Blocking a save on a budget.** Discussed above at length, and the answer never
moved.

**A `receiptless` value of "unknown".** Three states where two will do. The flag
means "do not expect a receipt for this"; not having ticked it means nothing has
been claimed either way, which is already the third state.

---

## Open questions

**Weekly budgets use ISO weeks, Monday-start.** That is what a phone calendar
shows and what `date.isocalendar()` gives. If someone in this house thinks of
the week as starting on Saturday — which is the Egyptian working week — this is
wrong for them and there is no setting for it. Left alone until somebody says so.

**Thumbnails are square and cropped to fill.** A till slip is a ribbon and gets
its middle shown. That is fine for recognising *which* receipt it is, which is
what the gallery is for, but it is worth revisiting if the gallery ever becomes
the way people actually read them.

**`check-limits` uses the household's default timezone**, not any individual's,
because a sweep has no session. Being an hour out either side of midnight costs
nothing when the same run happens again next hour, but a weekly budget's window
does move by a day at the boundary for someone in a very different zone.

**Nothing warns when `uploads/` gets large.** The backup script tars it, and a
household that photographs every receipt for two years will notice eventually.
A size figure on the Setup screen would be cheap. It is not there yet.
