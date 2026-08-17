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

---

## Two things changed after the phases landed

### The account box stopped saying "Auto"

The Account dropdown on the entry form opened with a blank option labelled
`Auto`, meaning "whatever the merchant's default is, or failing that the first
account". It was a label for a real answer the form declined to show you — and
on the one screen where speed matters, an invisible answer is the expensive
kind. It now names the account that will actually be used, pre-selected to the
same one `_prepare()` would have fallen back to, and tapping a merchant switches
it in front of you.

**Category kept its Auto**, which is not an inconsistency. A transaction always
comes out of exactly one account, so there was never a genuine blank there. "No
category" is a real state a row is allowed to stay in.

The non-obvious part was in `entry.js`. It decided "has the person chosen this
themselves?" by checking whether the select was empty — which works only while
an empty state exists. Removing the blank option would have made the box look
permanently chosen and silently killed merchant defaults for the account
altogether, with JavaScript *on*. It tracks a `touched` flag instead, set from
the `change` event, which fires for a human and never for a script assigning
`.value`. `verify_phase1.py` now checks that `!account.value` does not come back.

One honest cost: with JavaScript off, a merchant's default account no longer
applies, because the form always posts a real id. What you see is what is used.
That is a worse default and a better lie-to-truth ratio — the account is on
screen and can be changed before saving, which is more than "Auto" ever offered.

The transfer form's **Into account** keeps its `—`. That one is genuinely
undecided until you pick, and defaulting it would invent a movement of money
between two real accounts.

### Delete asks first

The delete button removed the entry on the first tap. It sits directly under
fields people edit on a phone, there is no undo on that path — the toast's Undo
only covers the ten minutes after a save — and since Phase 2 a receipt photo
goes with it.

It is now a link to a confirmation page, built exactly like the sign-out screen
and for the same two reasons: the CSP has no `unsafe-inline`, so a JavaScript
`confirm()` would silently not run, and a page keeps working with scripting off.
The page shows the amount, the merchant, the account, the date and the note — so
the second tap is spent reading the entry rather than reading the word "sure?" —
and says how many photos go with it.

Being a link rather than a form also means the Enter key inside the fields above
can no longer reach it at all, which the old separate-`<form>` trick only
half-solved.

---

## The round after: six things from using it

Nothing here is a phase. It is what the first week of real use turned up, and
two of the six were bugs that had been sitting in the schema since Phase 1.

### A transfer that was out by a factor of fifty-five

10.00 EGP left a bank account and 10.00 EUR arrived in another. Both numbers
were individually valid, both currencies were individually right, and nothing
anywhere objected.

Two things were wrong. The first was on the edit screen: change the destination
to an account in a different currency and the arriving amount stays in the box,
still holding a number about the currency it is no longer in. `ledger-edit.js`
now wipes it, and `entry.js` does the same and offers the cached rate's answer
as a starting point.

But JavaScript is a convenience here and the rule has to hold without it, so the
real fix is `transactions._check_transfer_rate()`. It converts both sides to base
with the cached rates and refuses a tenfold disagreement with a sentence saying
how far out it is.

Deliberately generous, and deliberately silent when the cache cannot answer. The
app does not compute the arriving amount and should not — a bank's rate on the
day, with its spread and its fee, is not the mid-market rate and only the person
holding the statement knows it. So this checks *plausibility*, never
correctness. A guard that fires on a real transfer is worse than no guard: people
learn to work around it, and then it catches nothing. 550 EGP arriving as 9, 10
or 12 EUR all pass.

One consequence worth writing down: **the guard is only as awake as the cron
entry**. An empty `fx_rates` means nothing to compare against and nothing said.

### The Month tab said nothing had happened

A ledger with two income entries and one transfer in it, and the month screen
read "Nothing logged this month". Technically true of *spending*, and a useless
thing to tell somebody who had logged three things that morning.

"No spending" and "no activity" are different sentences and only one of them was
true. The screen now carries income and the net beside the spend total whenever
there is any income, and the empty state distinguishes the two cases — including
counting what *was* logged, so it is obvious the entries landed.

Transfers are counted and never totalled. Moving your own money between your own
accounts is neither income nor expenditure, and adding it to either would count
the same money twice.

### The merchant field on a transfer

A transfer moves money between two of your own accounts. There is no
counterparty to name, and `_prepare()` has dropped the field on one since Phase 1
— so the edit screen was offering a control that quietly did nothing, which is
exactly what the "nothing dead ships" rule is about. It is hidden server-side
when a transfer is opened, which is the case that matters and the one that works
with scripting off, and `ledger-edit.js` covers switching the type in place.

### The list opens on you

Your own spending is the question you have nine times out of ten, so
`/transactions` filters to the reader by default.

The awkward part is what that does to "no filter". A missing `user_id` used to
mean everyone and now means you, so not-filtering had to become something you
can *say*: `user_id=all`. That keeps the unfiltered list a link — the querystring
is still the whole of the filter state, and a filtered view is still something
you can bookmark or send to somebody.

The default does not count towards the "3 filters applied" badge. A badge that is
never zero is a badge nobody reads.

### An account can belong to more than one person

`accounts.owner_id` held exactly one user, and a joint current account belongs to
both people. "Shared" (NULL) was the only way to say "both", which threw the
information away rather than recording it.

`account_owners` replaces it. The old column is retired the way
`merchants.is_system` was in 004 — left in place because 001 is immutable and
rebuilding a table six triggers reference is the bigger risk — except this time
with triggers that *refuse* a write to it. Two sources of truth about who owns
what would disagree within a week, and the disagreement would be silent.

The thing worth being loud about: **ownership is not visibility, and 006 did not
make it one.** Section 4 keys off the transaction's owner and never off the
account's, precisely so a shared bank account does not expose one person's
spending to everyone who draws on it. Putting two people on an account changes
whose list it appears in. It shows neither of them a purchase the other made
privately, and `verify_myaccounts.py` checks exactly that rather than trusting
the sentence.

### My accounts, and the balance walk

The account history page was on the "deliberately left out" list in Phase 1, on
the grounds that the list's account filter answered the same question. That
stopped being true the moment the question became "how did the balance get
here", which a filtered list cannot answer at all.

The walk goes **backwards from the balance now**, and that direction is the whole
design. Forwards from the opening balance would accumulate every rounding
decision and every unconvertible leg, and would disagree with the number on the
accounts screen by the time it reached the top — two screens showing the same
account and disagreeing is worse than either being slightly off.

Both legs of a transfer are counted per row, so money moved between a card and
the account behind it nets to zero instead of appearing as a movement that never
happened.

And this is where section 4 and the balance exception meet head on. Filtering the
arithmetic prints a wrong balance. Unfiltering the list hands you somebody's
private purchases. Neither can win, so the walk reads everything, the list shows
only what you may see, and a marker row says how many entries were skipped
between two visible ones. The step in the number is already on screen; the marker
only stops it looking like a bug. It says a count and nothing else — no whose,
no what, no how much.

Pagination is a link in fixed steps that says how many are left, rather than an
infinite scroll. It needs no JavaScript and it is honest about the cost of the
walk.

### What was rejected this round

**A modal for the account summary.** It was asked for as a pop-up, and a page is
what shipped: on a phone a full screen with a working back button *is* the
pop-up, and a `<dialog>` needs `showModal()` — so with scripting off the content
would simply not exist.

**Letting the account owners decide who sees transactions.** It would make the
"My accounts" screen more powerful and it would quietly gut section 4. The two
rules are kept apart on purpose and the templates say so where someone is
choosing owners, because that is the screen where the wrong assumption gets made.

**Computing the arriving amount of a transfer.** The cached rate fills the box as
a starting point and is never what gets stored. The rate on the day of the
transfer is the bank's, not the mid-market one, and the app has no way to know
it.

### And the one that arrived by traceback

`git pull` brought migration `006`, the dev server reloaded on the file change,
the database did not reload with it, and the Accounts tab answered with
`sqlite3.OperationalError: no such table: account_owners` from three frames
inside a view.

Every part of that is true and none of it is useful. It names the table rather
than the command that creates it, it arrives as a stack trace on a screen meant
for someone checking their spending, and it only fires on whichever page happens
to touch the new table first — so an app that is entirely broken looks partly
fine.

The app now compares the migrations on disk against `schema_migrations` at boot
and refuses every request while it is behind, with a page that says which files
have not run and the exact command. Three details in it are deliberate:

- **Checked once at startup**, so the healthy path costs nothing, and re-checked
  per request *only while it is failing* — which means `flask migrate` in
  another window fixes it without a restart.
- **`outstanding()` is read-only**, unlike the existing `applied()`, which
  bootstraps `schema_migrations` as a side effect of looking. Asking a question
  on every boot must not change the answer, and starting the app must not
  conjure `app.db` into existence.
- **Static files are exempt**, so the page arrives styled rather than as raw
  HTML — which would read like a crash, which is the impression this exists to
  remove.

A fresh install falls out of the same rule: nothing is applied, everything is
outstanding, and the first page anyone sees tells them to migrate.

---

## Three more from using it

### Three numbers that would not add up

"Spent 50, came in 6,040" sat above a balance of 5,387, and the screen offered
no way to get from one to the other. Both figures were right. So was the
balance. Two things were missing and one was mislabelled:

- an **opening balance of −3**, never shown anywhere;
- a **600 transfer out**, excluded from both figures on purpose — moving your own
  money is neither spending nor income — and therefore invisible;
- and the two figures were **this month** while the balance was **all time**, a
  distinction the screen never made.

The fix is a sum, all time, ending on the balance: opened with, income,
spending, moved out, moved in, balance. It is arithmetic rather than a stored
total, so a line cannot be added without appearing, and `reconcile()` compares
its own result against `balances.py` and the template says so if they ever
disagree. The month card now says "this month only" out loud.

Worth stating plainly: nothing was miscalculated. The screen was showing a
person two of five terms and expecting them to trust the answer, which is a
worse failure than an arithmetic bug because it looks like one.

### Spending is red now

It used to stay ink, on the argument that most rows are spending and colouring
the ordinary case leaves nothing to mean "look at this". That argument was
wrong. Money leaving is what this app is about, and a column where every sign
carries a colour scans faster than one where you have to read the minus. It
reuses `--over`, so an overdrawn balance and a payment are the same red rather
than a second one nobody chose.

### The balance history became a timeline

A column of numbers answers "what was the balance after that one" and answers
nothing at all about shape. So the window is now also a graph, with a slider
that walks a marker along it and a readout of what each entry was.

Every coordinate is computed in `accounts._series()`. The script moves a marker
along a line that is already in the HTML and does nothing else — which is what
keeps it honest with scripting off: the shape still draws, the list still reads,
and the slider ships `disabled` so the script is what enables it rather than a
dead control sitting there.

Two details that are not stylistic:

- **The money strings are rendered server-side onto each point.** Formatting in
  the browser would be a second implementation of `money.py` — the exponent
  table, the rounding, all of it — in a language with one number type. That is
  how a screen starts disagreeing with the database it is reading.
- **The chart reads oldest-first, the list newest-first.** Time goes left to
  right; "what did I just spend" belongs at the top of a page. The series is the
  list reversed once, server-side, so the two cannot be built from different
  queries and drift.

And one bug worth remembering, because every assertion passed while it was
broken. The points ride in a `data-points` attribute, and it shipped
double-quoted. Flask's `tojson` escapes `'` as `'` and leaves `"` raw —
it is built for a single-quoted attribute — so the JSON was truncated at the
first inner quote, `JSON.parse` threw, and the slider moved its thumb while
changing nothing. Every byte the checks grepped for was present. The check now
pulls the attribute out and parses it, and asserts one point per row the list
renders.

### Noticed, not fixed

`Banque Misr` has an opening balance of −3.00, and migration `003` forbids a
non-credit-card opening below zero. Both are true because the row predates the
trigger: **003 added the rule and did not check the rows already there.** The
account can be corrected in Setup → Accounts, which will now refuse to save it
that way. Worth knowing as a general shape — a constraint introduced later only
binds what happens next, and nothing in this project audits history when a rule
arrives.

---

## Three nitpicks, and what each one turned out to be about

### A third colour

Income was teal, spending had just gone red, and transfers were `--text-muted`.
Grey was fine when it meant "the quiet one" among two; next to a red it reads as
*disabled*. Transfers now have `--move`, a violet, defined in both schemes.

Three directions, three deliberate colours, none of them a default.

### The confirmation would not leave

The toast is `position: fixed` above the tab bar, and it stayed for as long as
the page was open — which on the one screen built to be used standing at a till
means it sits on top of whatever you reach for next.

It now retires itself after twelve seconds, and stops counting down if you point
at it or tab into it, because reaching for something is the clearest possible
signal that you want it. There is also a `×`, which is a plain link back to the
form without `?saved=` — so a browser with scripting off has the same exit, and
anyone who wants it gone now does not have to wait.

Worth being clear that Undo did not get shorter: it still works for ten minutes
server-side, and an entry can always be edited or deleted from Entries. The
toast leaving is the *shortcut* expiring, not the entry setting.

### The edit screen was a wall

Twelve fields, all visible, three or four of them meaningless for whatever you
were actually editing. A plain spend was being asked "into account?", "amount
that arrived?" and "rate → EGP?". A transfer was offered a merchant it can never
have, and an in-person/online toggle for a counterparty that does not exist.

Two changes, and the first is the one that mattered: **the fields that do not
apply are gone rather than empty.** An empty box is a question; a question about
something that cannot be true is noise, and noise is what "cluttered" means. A
plain spend now shows eight fields and a transfer shows seven, from the same
template.

The second is grouping — Money, then what it was, then Notes — so what remains
reads as three short cards instead of one column of twelve.

Everything is decided server-side from the stored row, which is the state that
matters and the one that survives scripting being off: open a transfer and the
transfer fields are there; open a spend and they never were. `ledger-edit.js`
only keeps up when the type or currency is changed *while the page is open*,
which the server cannot see until you save.

Nothing about what gets stored changed. It is still one form, one POST, and
`transactions._prepare()` — the same function the entry screen goes through.

---

## The transfer currency field, and what it was hiding

The question was "what exactly does the currency field represent?" on a transfer
between an AED account and a EUR one. The honest answer turned out to be
*nothing*, and chasing that removed a stored column's worth of fiction.

### What leaves an account is in that account's currency

There is no such thing as taking euros out of an Egyptian account. The bank
converts and what left the account was pounds. The form was letting you say
otherwise, and `_prepare()` believed it — which records the *destination's*
amount on the source leg and then needs an exchange rate to undo, describing the
conversion twice.

It now takes the source account's currency rather than reading the box, and the
box is gone from both screens. The code moved onto the Amount label, and on the
till display the currency mark follows the account instead of being frozen at
the household's own — it was reading "E£ 1000" over an amount that was dirhams.

This is exactly the shape of the earlier data: a transfer stored as 10.00 **EUR**
out of an **EGP** account, with a rate of 60 attached to make the balance come
out. The balance was right. The encoding was describing the conversion on the
wrong leg.

### And therefore no rate to base

Once both legs are in their own account's currency, the two places that convert
— `balances._foreign_legs()` and `accounts._effect()` — only ever convert a leg
whose currency differs from the account holding it, which can no longer happen
on a transfer. The rate to base is a stored number nothing reads.

So a transfer is not asked for one and does not store one. That is the "nothing
dead ships" rule reaching a column, and it settles the second half of the
complaint — the rate to EGP was not merely useless on that screen, it was
useless full stop. On a **spend** it stays exactly as it was: an Egyptian card
charged in euros is real, and the month total has to value it.

Rows written before this keep their rate and still convert. The old shape is
handled, it is just no longer created — and there is a check asserting the
property (`t.currency = a.currency` on every new transfer leg) rather than only
the consequence.

One existing check asserted the opposite — that the rate was captured on a
transfer. It was updated rather than deleted, with the reason in its label,
which is what the house rule about a rule changing on purpose is for.

### What a transfer actually has a rate for

Its two accounts. The entry form asks for that instead: "Rate AED → EUR",
two-way with the arriving amount, because a bank quotes a rate and dividing at a
counter is not a thing anyone should have to do.

It is stored nowhere — it is the arriving amount over the amount, and those two
are what get saved. The input has no `name` and cannot post, so no validation
and no schema changed. It is hidden until the script unhides it, so a browser
with scripting off never meets a control that would do nothing for it; the
arriving amount is the field the server reads either way, and it is directly
above. The edit screen states the same rate as a sentence, derived server-side
from the two stored numbers, with no script at all.

### Two more found in a screenshot

Both while every check above was already passing.

The rate-to-base field was being un-hidden again after the direction changed,
because three different call sites reach `syncFx()` and whichever ran last won.
The test belongs *inside* `syncFx`, not at its callers. And the till display's
currency mark was static, so a 1000 AED transfer was captioned `E£`.

Assertions check what you thought to ask, and neither of these was a thing I
thought to ask.
