# Phase 1 — the entry form and the account model

Status: **the entry form, the reference data behind it, and the account model are
done.** The transaction list, filters, edit/delete, account balances and the
monthly summary are the rest of Phase 1 and have not been started — see
[Where this goes next](README.md#where-this-goes-next).

Verified by `verify_accounts.py` (116 checks) and `verify_phase1.py` (67), with
`verify_phase0.py` (56) and `verify_auth.py` (25) still passing. 264 in total; all
four build throwaway databases and touch nothing in `app.db`.

This file is the *why*. The rules themselves live in `README.md`; what follows is
what was considered and rejected, which is the part that is expensive to
reconstruct later.

---

## Part one — the entry form

The bar from section 6 is *type amount, tap chip, tap save*. Everything else on
the screen follows from defending that.

- **The amount is a point-of-sale display.** Oversized tabular figures, currency
  mark small and set apart, chips directly beneath. It is the one loud surface in
  the app; the spec asked for boldness in exactly one place and this is it.
- **Merchant chips are radio inputs**, not buttons, so the whole path works with
  JavaScript switched off.
- **Merchant defaults are applied twice**: instantly by JavaScript when you tap a
  chip, and again server-side for any field left blank. The second one is what
  makes the no-JavaScript path fill itself in rather than saving blanks.
- **Explicit choices are never overwritten** by a merchant default, in either path.
- **After saving you land back on a blank form**, with a toast showing amount and
  merchant plus Undo. The toast is server-rendered, so Undo survives a refresh.

### Measured

The fastest path costs **~0.5s of machine time** (380px viewport, 120ms latency,
1.5 Mbps — worst of five runs 0.53s). That is the app's share of the ten-second
budget; the rest is a human typing. Round-trips per entry: one.

### Decisions

| Decision | Why |
|---|---|
| **CSRF tokens on every unsafe method**, no dependency, no exemption list | `SameSite=Lax` covers most of the threat but is not something to bet a money app on, and an exemption list is how one endpoint ends up unprotected two phases later. ~30 lines in `csrf.py`. |
| **The entry form owns `/`; the dashboard moved to `/dashboard`** | "This screen *is* the product." The root route should be it. |
| **Camera slot reserved, no control shipped** | Receipts are Phase 2. The `.pos__aux` grid column is zero-width today so the button drops in without reflow, and nothing on screen lies about what it does. |
| **Same-currency transfers copy the amount across automatically** | Asking twice is friction with no information in it. Cross-currency still asks, because that number is not derivable. |
| **Undo is owner-only and expires after 10 minutes** | Wide enough to catch a fat-finger, narrow enough that it never becomes a deletion tool wearing a friendlier label. Admin is deliberately *not* special here. |
| **Foreign-currency entries require the rate at entry** | Section 2 says the rate on the day is unrecoverable afterwards. The field only appears when the currency is not the base, so it costs the common path nothing. |
| **`is_online` defaults to "In person" and is a select, not a checkbox** | A checkbox submits nothing when unchecked, which makes "use the merchant default" impossible to distinguish from "explicitly offline". |
| **Nothing hard-deletes** | Accounts and categories carry transactions; a merchant you stop using should leave the chips without rewriting last March. |

### Bugs found and fixed during the build

- **`[hidden]` was being overridden.** `.field { display: block }` and
  `.grid-2 { display: grid }` beat the UA's `[hidden] { display: none }` on
  specificity, so the FX-rate field and the transfer fields were visible on a
  plain spend. Everything JavaScript hides on this form goes through that
  attribute, so the one-line fix is load-bearing.
- **The autofocus ring was a heavy box** around the POS display on every single
  page load. Now an underline — still a clearly visible focus affordance, not the
  loudest thing on the screen.

---

## Part two — the account model, cash, and receipts

Migrations `003` and `004`. The theme running through all of it: facts that were
welded together get pulled apart, and each half gets somewhere honest to live.

### Chips had to toggle both ways

A radio group cannot be emptied by clicking one of its own buttons, so a mis-tap
on a merchant chip was unfixable without reloading the page. The label click is
now intercepted and the toggle handled by hand: tapping the selected chip points
the group at an empty **No merchant** option, and un-fills exactly what that
merchant auto-filled — comparing the value before clearing, so a choice made by
hand is never wiped by de-selecting a chip that happened to share it.

**No merchant** is a real chip rather than a script-only gesture because that is
also the answer with JavaScript off, where a second tap does nothing.

### One merchant box, not two

The search box and the "type a new merchant name" box did the same job from two
places. The search box now carries the `new_merchant_name` field itself: it
filters the list, and if nothing matches it *is* the new merchant's name. With
JavaScript off, typing and pressing Save creates it server-side; the Add button
only saves the round trip.

### Receipts are not merchants

The seeded **Receipt-less** merchant welded two independent facts together. A
street vendor with no name and no paper is one row; a named shop that hands over
nothing is another; a nameless stall that *does* print a slip is a third. Picking
"Receipt-less" from a list of merchants forced a choice between recording who you
paid and recording whether you can prove it.

`004` retires the merchant and adds `transactions.receiptless`, with existing
rows migrated (`receiptless = 1`, `merchant_id = NULL`) so nothing is lost. The
entry form has two independent controls; the receipt toggle keeps the left-edge
position the old chip had, and survives a transfer, because a cash withdrawal has
a slip.

Retiring rather than keeping the merchant was the call: leaving it in the list
would mean two ways to say the same thing, and the one that survives is the one
that composes. `merchants.is_system` is now a column with no rows in it — 001 is
immutable, and a table rebuild to drop one unused column is not worth it.

### Spending and income are different lists

`merchants.kind`. The chip row is the till path, so it shows **spending only**;
income sources live under the search box with everything else, because income is
logged rarely and deliberately and should not compete for the fast row. Switching
direction swaps the list, swaps the field label, and clears a selection carried
over from the other side. A merchant added inline joins the list for whichever
side you were on.

An existing name is reused rather than reclassified: silently flipping a shop into
an income source because someone typed it while logging a salary is worse than a
merchant appearing where it was not expected, and Setup fixes it in one tap.

### Links: a parent pointer, one level deep

A debit card and an Instapay handle are *ways of reaching* an account, not second
pots. Modelled as `parent_account_id` with triggers rather than form validation,
because the form is not the only thing that will ever write.

- **One Instapay per account, unlimited debit cards** — a partial UNIQUE index is
  the entire rule.
- **A linked account takes its parent's currency**, and a currency change on the
  parent propagates. Letting them differ invites a balance that is the sum of two
  units.
- **Instapay carries no opening balance**: that money is already counted in the
  account behind it.
- **"Belongs to" defaults to the parent's owner** — a card on someone's account is
  theirs — but stays editable, because a joint account with a card each is real.

Rejected: making Instapay a boolean flag on the parent account. It needs its own
handle, its own name and its own transactions, and a flag would have meant
special-casing every one of those.

### Cash, and what a withdrawal is

Cash records spending, income and reimbursements, and nothing transfers out of
it. Money into cash is a **withdrawal**, and a withdrawal happens on a specific
card, with that card's ceiling, and the card is what the statement will show. So
a transfer into cash from a bank, wallet or Instapay is refused with the linked
cards named, and Instapay resolves to its parent's cards.

This is the one place the app argues back. It is worth it because "the bank gave
me cash" is never the whole truth, and the lost detail is exactly the one needed
to reconcile against a statement later.

**Reimbursements are `income`** filed under a seeded Reimbursement category, not
a fourth direction. Mechanically identical, and a fourth direction means
rebuilding the `transactions` table to change a CHECK constraint. Worth revisiting
only if reimbursements turn out to need fields income does not have.

### Card details, and limits as periods

Network, colour, expiry and a withdrawal ceiling are required on both card types;
credit cards add a monthly local and international limit, with a tick for the
banks where they match — unticked by default, because in Egypt they usually do
not.

The limits are **periods**, not per-transaction checks: the withdrawal ceiling is
a calendar day, the credit limits a calendar month, and both are enforced by
summing the period so the refusal can say what is left. Local versus
international is split by "a charge in the card's own currency is local", which is
the only honest signal available without a merchant country — it is written down
here because it is a judgement call someone will want to revisit.

The international limit is **not** enforced on a card denominated in something
other than the base currency: each transaction carries its rate *to base*, so
summing foreign charges lands in base, and comparing that against a ceiling set in
another unit is arithmetic on two different units. It declines rather than
guesses.

**Colour is free text** validated against a hex-or-plain-name pattern, and painted
as an SVG `fill` attribute. An inline `style` would have been the obvious way and
is exactly what the CSP refuses — which also turned up four pre-existing inline
styles in the settings templates that had never rendered.

**Expiry deactivates the card** rather than warning about it, checked wherever
accounts are read, because a card expires whether or not anyone opens Setup. It
is a SELECT first and a write only when there is something to write: this runs on
every entry-form load and a pointless UPDATE per page view is a real cost on an SD
card.

### Rates, fetched weekly, never during a request

`fx.py` is the only code that reaches the public internet, and it runs from cron.
A rate lookup inside the entry form would trade the app's one hard promise — that
it works with the internet unplugged — for a number a human can supply.

The cache is a **default**, not a record: what is stored against a transaction is
whatever was in the box at save time. The staleness guard (`--max-age-days`,
default 7) means a *daily* cron produces *weekly* rates, survives the Pi being
asleep, and never hammers a provider. A failed fetch is logged and shrugged off.

### Smaller things

- **Sign out asks first**, as a page rather than a `confirm()` — the CSP forbids
  inline handlers, so a page is the only version that also works with scripting
  off. Looking at the question does not end the session.
- **Settings rows highlight on hover**, behind `@media (hover: hover)` so a phone
  — where hover means "the last thing tapped" and sticks there — never shows it.
  `:active` covers touch and `:focus-visible` the keyboard.
- **Emoji for account type and currency** are scanning aids and never the only
  signal: the type and currency code are always spelled out beside them, because
  Windows has no flag font and shows boxed letter pairs.

---

## Open questions for the rest of Phase 1

- **The chip row is "recent", not "most used".** Section 6 says recent, so that is
  what it does — scoped to your own transactions, which is stricter than the
  visibility rule and therefore cannot leak. Worth revisiting after a month of
  real data; frequency may predict better than recency.
- **No "quick amounts".** Deliberately: they would be guesses until there is real
  data to derive them from.
- **Income shares the spend form.** If income entry turns out to want different
  fields, it should split.
- **Overdraft is not prevented live.** Rule 2's balance half is enforced on
  opening balances only. Enforcing it per transaction needs the balance engine,
  and switching it on before opening balances are entered would block the very
  first entry.
- **An instapay balance should be its parent's.** Structurally the link is in
  place; the arithmetic arrives with the balance engine, and it is the one part of
  "changes to Instapay also happen to the bank" that is not yet visible anywhere.
- **With JavaScript off, both merchant lists render at once.** Hiding the wrong
  one server-side would make income sources unreachable without a page reload.
  The server refuses a merchant from the wrong list, so it cannot produce bad
  data — it is only noisier.
