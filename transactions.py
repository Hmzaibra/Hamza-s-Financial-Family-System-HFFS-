"""Transaction creation and the queries the entry form needs.

Every write goes through create_transaction(). There is deliberately no second
path: the rules about positive integer money, direction/counter-account
coherence and inherited sharing are enforced in one function, and the database
CHECK constraints behind it are the backstop rather than the only guard.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from db import base_currency, execute, query, query_one, today_for, utc_now
from money import MoneyError, convert_to_base, format_minor, parse_to_minor
from visibility import can_edit

DIRECTIONS = ("spend", "income", "transfer")

# The two account types that can physically hand over banknotes. A bank account
# does not dispense cash; one of its cards does, and which card it was is the
# part worth recording.
CARD_TYPES = ("credit_card", "debit_card")

# Types that can hold linked cards, and the types that can be linked to them.
PARENT_TYPES = ("bank", "wallet")
LINKABLE_TYPES = ("instapay", "debit_card")

# Purely to make a list of six similar rows scannable at a glance. Never the
# only signal: the type is always spelled out next to it, because these render
# as boxes on any system without an emoji font.
ACCOUNT_EMOJI = {
    "bank": "🏦",
    "wallet": "👛",
    "cash": "💵",
    "credit_card": "💳",
    "debit_card": "🏧",
    "instapay": "📲",
}


def account_emoji(account_type: str) -> str:
    return ACCOUNT_EMOJI.get(account_type or "", "")

# How long after saving the toast's Undo still works. Long enough to catch a
# fat-finger, short enough that it is never a deletion tool in disguise.
UNDO_WINDOW = timedelta(minutes=10)

# Columns every account lookup needs. The type and the link drive the cash rules
# below, so no code path is allowed to fetch an account without them.
_ACCOUNT_COLUMNS = (
    "id, name, type, currency, owner_id, is_active, parent_account_id, "
    "withdrawal_limit_minor, credit_limit_local_minor, credit_limit_intl_minor, "
    "card_expires_on"
)


class EntryError(ValueError):
    """A problem worth showing the person who is standing at a till."""

    def __init__(self, message: str, field: str = ""):
        super().__init__(message)
        self.field = field


# ------------------------------------------------------------------ lookups


def active_accounts() -> list:
    return query(
        "SELECT id, name, type, currency, owner_id, sort_order, parent_account_id "
        "FROM accounts WHERE is_active = 1 ORDER BY sort_order, name"
    )


def deactivate_expired_cards(today: date) -> list[str]:
    """Switch off any card whose printed month has passed. Returns their names.

    A card expires whether or not anyone opens the settings screen, so this runs
    where accounts are read rather than where they are edited. It is a SELECT
    first and a write only when there is something to write: this is on the path
    of every entry-form load, and a pointless UPDATE per page view is a real cost
    on an SD card.

    A card is good through the end of its printed month, so the comparison is on
    'YYYY-MM' and strictly less-than.
    """
    month = today.strftime("%Y-%m")
    expired = query(
        "SELECT id, name FROM accounts WHERE type IN ('credit_card','debit_card') "
        "AND is_active = 1 AND card_expires_on IS NOT NULL AND card_expires_on < ?",
        (month,),
    )
    if not expired:
        return []
    execute(
        "UPDATE accounts SET is_active = 0 WHERE id IN "
        "(SELECT id FROM accounts WHERE type IN ('credit_card','debit_card') "
        " AND is_active = 1 AND card_expires_on IS NOT NULL AND card_expires_on < ?)",
        (month,),
    )
    return [row["name"] for row in expired]


def linked_cards(account_id: int) -> list:
    """The debit cards that draw on this account.

    Called with a bank or wallet id. Instapay resolves to its parent first —
    an Instapay handle has no cards of its own, it shares the ones belonging to
    the account behind it.
    """
    return query(
        "SELECT id, name, type FROM accounts "
        "WHERE parent_account_id = ? AND type = 'debit_card' AND is_active = 1 "
        "ORDER BY sort_order, name",
        (account_id,),
    )


def active_categories() -> list:
    """Parents with their children, flattened for a grouped <select>."""
    return query(
        "SELECT c.id, c.name, c.parent_id, c.icon, p.name AS parent_name "
        "FROM categories c LEFT JOIN categories p ON p.id = c.parent_id "
        "WHERE c.is_active = 1 "
        "ORDER BY COALESCE(p.sort_order, c.sort_order), COALESCE(p.name, c.name), "
        "         c.parent_id IS NOT NULL, c.sort_order, c.name"
    )


def active_merchants(kind: str) -> list:
    """The merchants that belong to one side of the form.

    Who you buy from and who pays you are different lists that happen to share a
    table. Mixing them means an employer sits in the chip row while you are
    standing at a till, which is noise at the exact moment noise is expensive.

    There is deliberately no unfiltered variant: a caller that does not say which
    side it is asking about is a caller that is about to mix them again.
    """
    return query(
        "SELECT id, name, default_category_id, default_is_online, default_account_id, kind "
        "FROM merchants WHERE is_active = 1 AND kind IN (?, 'both') ORDER BY name",
        (kind,),
    )


def recent_merchants(user_id: int, kind: str = "spend", limit: int = 7) -> list:
    """Merchants this person has used lately on this side of the form.

    Scoped to the caller's own transactions rather than to everything the
    visibility rule would let them see. That is stricter than section 4, so it
    cannot leak; it is also the right answer, because the chips are meant to
    predict *your* next purchase, not the household's.
    """
    return query(
        "SELECT m.id, m.name, m.default_category_id, m.default_is_online, "
        "       m.default_account_id, m.kind, MAX(t.created_at) AS last_used "
        "FROM transactions t JOIN merchants m ON m.id = t.merchant_id "
        "WHERE t.user_id = ? AND m.is_active = 1 "
        "  AND m.kind IN (?, 'both') AND t.direction = ? "
        "GROUP BY m.id ORDER BY last_used DESC LIMIT ?",
        (user_id, kind, kind, limit),
    )


def find_or_create_merchant(name: str, kind: str = "spend") -> int:
    """Inline 'Add <name>' from the entry form.

    The UNIQUE(name) COLLATE NOCASE index means a race between two phones adds
    the merchant once; the loser of the race re-reads it instead of erroring.

    An existing name is returned as-is even if it was created on the other side
    of the form. The alternative — silently flipping a shop into an income
    source because someone typed it while logging a salary — is worse than a
    merchant appearing where it was not expected, and the settings screen can
    correct it in one tap.
    """
    name = (name or "").strip()
    if not name:
        raise EntryError("Merchant name cannot be empty.", "merchant")
    if len(name) > 80:
        raise EntryError("That merchant name is too long.", "merchant")
    if kind not in ("spend", "income"):
        kind = "spend"

    existing = query_one("SELECT id FROM merchants WHERE name = ?", (name,))
    if existing:
        return existing["id"]

    try:
        cur = execute(
            "INSERT INTO merchants (name, default_is_online, is_system, is_active, kind, "
            "created_at) VALUES (?, 0, 0, 1, ?, ?)",
            (name, kind, utc_now()),
        )
        return cur.lastrowid
    except Exception:
        again = query_one("SELECT id FROM merchants WHERE name = ?", (name,))
        if again:
            return again["id"]
        raise


# --------------------------------------------------------------- validation


def _int_or_none(raw, field: str) -> int | None:
    raw = (raw or "").strip() if isinstance(raw, str) else raw
    if raw in (None, ""):
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        raise EntryError("That selection wasn't understood.", field) from None


def _account(account_id: int | None, field: str):
    if account_id is None:
        raise EntryError("Pick an account.", field)
    row = query_one(f"SELECT {_ACCOUNT_COLUMNS} FROM accounts WHERE id = ?", (account_id,))
    if row is None:
        raise EntryError("That account no longer exists.", field)
    if not row["is_active"]:
        raise EntryError(f"{row['name']} is archived.", field)
    return row


def _check_cash_withdrawal(account, amount_minor: int, currency: str, occurred_on: str,
                           exclude_id: int | None = None) -> None:
    """Money landing in cash came out of a card, and the card is the record.

    "The bank gave me cash" is never the whole truth: an ATM or a counter
    withdrawal happens on a specific card, with that card's ceiling, and the
    card is what the statement will show. So a transfer into a cash account has
    to name one, and a bank, wallet or Instapay source is bounced back with the
    cards it could have meant.
    """
    if account["type"] in CARD_TYPES:
        limit = account["withdrawal_limit_minor"]
        # Comparable only in the card's own currency. Converting here would mean
        # applying today's rate to a ceiling the bank set in its own terms.
        if limit and currency == account["currency"]:
            # The ceiling is per day, which is how banks actually set it — so it
            # is the day's running total that matters, not this one withdrawal.
            taken = query_one(
                "SELECT COALESCE(SUM(t.amount_minor), 0) AS taken FROM transactions t "
                "JOIN accounts a ON a.id = t.counter_account_id "
                "WHERE t.account_id = ? AND t.direction = 'transfer' AND a.type = 'cash' "
                # An edit must not be measured against its own old amount, or
                # raising 500 to 600 reads as 1100 against the day's ceiling.
                "  AND t.occurred_on = ? AND t.currency = ? AND t.id <> ?",
                (account["id"], occurred_on, currency, exclude_id or -1),
            )["taken"]

            if taken + amount_minor > limit:
                code = account["currency"]
                left = limit - taken
                if left <= 0:
                    raise EntryError(
                        f"{account['name']} has already reached its "
                        f"{format_minor(limit, code)} {code} daily cash withdrawal limit.",
                        "amount",
                    )
                raise EntryError(
                    f"{account['name']} has a {format_minor(limit, code)} {code} daily cash "
                    f"withdrawal limit — {format_minor(left, code)} left today.",
                    "amount",
                )
        return

    if account["type"] == "cash":
        raise EntryError("Cash is already cash — there is nothing to withdraw.", "counter_account")

    # Instapay has no cards of its own; the parent's cards are the answer.
    holder_id = account["parent_account_id"] or account["id"]
    holder = account
    if account["parent_account_id"]:
        holder = query_one(
            f"SELECT {_ACCOUNT_COLUMNS} FROM accounts WHERE id = ?", (holder_id,)
        ) or account

    cards = linked_cards(holder_id)
    if cards:
        names = ", ".join(card["name"] for card in cards)
        raise EntryError(
            f"{account['name']} cannot dispense cash. Log the withdrawal on the card "
            f"that did: {names}.",
            "account",
        )
    raise EntryError(
        f"{account['name']} cannot dispense cash. Link a debit card to "
        f"{holder['name']} in Setup → Accounts, then log the withdrawal on that card.",
        "account",
    )


def _check_credit_limit(
    account, amount_minor: int, currency: str, fx_rate, occurred_on: str, base: str,
    exclude_id: int | None = None,
) -> None:
    """A credit limit is a ceiling on a month, so a month is what gets summed.

    Local and international are separate numbers because Egyptian banks set them
    separately, and the app has exactly one honest way to tell the two apart: a
    charge in the card's own currency is local, and anything else is not.

    The international side is only enforced when the card is denominated in the
    base currency. Summing charges in three foreign currencies means converting
    them, each transaction carries its rate *to base*, and applying those to a
    ceiling set in something other than base would be arithmetic on two different
    units. Rather than guess, it declines to check.
    """
    if account["type"] != "credit_card":
        return

    international = currency != account["currency"]
    limit = account["credit_limit_intl_minor" if international else "credit_limit_local_minor"]
    if not limit:
        return
    if international and account["currency"] != base:
        return

    month = occurred_on[:7]
    rows = query(
        "SELECT amount_minor, currency, fx_rate_to_base FROM transactions "
        "WHERE account_id = ? AND direction = 'spend' AND occurred_on LIKE ? "
        # Same reason as the withdrawal ceiling: an edit is not competing with
        # the version of itself it is replacing.
        f"  AND currency {'<>' if international else '='} ? AND id <> ?",
        (account["id"], f"{month}-%", account["currency"], exclude_id or -1),
    )

    # Conversion in Python, in Decimal, never as SUM(amount * rate) in SQL —
    # that is float arithmetic on money through the back door.
    if international:
        spent = sum(convert_to_base(row["amount_minor"], row["fx_rate_to_base"]) for row in rows)
        charge = convert_to_base(amount_minor, fx_rate)
    else:
        spent = sum(row["amount_minor"] for row in rows)
        charge = amount_minor

    if spent + charge > limit:
        code = account["currency"]
        side = "international" if international else "local"
        left = limit - spent
        if left <= 0:
            raise EntryError(
                f"{account['name']} has used its {format_minor(limit, code)} {code} "
                f"monthly {side} limit for {month}.",
                "amount",
            )
        raise EntryError(
            f"{account['name']} has a {format_minor(limit, code)} {code} monthly {side} "
            f"limit — {format_minor(left, code)} left this month.",
            "amount",
        )


def _check_transfer_rate(amount_minor: int, currency: str,
                         counter_minor: int, counter_currency: str) -> None:
    """Refuse a cross-currency transfer whose two sides cannot both be true.

    This exists because of a real one: 10.00 EGP left a bank account and 10.00
    EUR arrived in another, and nothing anywhere objected. Both numbers were
    individually valid, the currencies were individually right, and the entry
    was wrong by a factor of fifty-five.

    The app deliberately does not compute the arriving amount — a bank's rate on
    the day, with its spread and its fee, is not the mid-market rate and only the
    person holding the statement knows it. So this checks *plausibility*, not
    correctness: both sides are converted to base with the cached rates and
    compared. A tenfold disagreement is not a bad rate, it is a different number.

    Deliberately generous, and silent when the cache cannot answer. A guard that
    fires on a real transfer would be worse than no guard at all — people would
    learn to work around it, and then it would catch nothing.
    """
    if currency == counter_currency:
        return

    import fx

    base = base_currency()
    rates = fx.cached(base)

    def to_base(minor: int, code: str) -> int | None:
        if code == base:
            return minor
        known = rates.get(code)
        return convert_to_base(minor, known["rate"]) if known else None

    left = to_base(amount_minor, currency)
    right = to_base(counter_minor, counter_currency)
    if not left or not right:
        # No cached rate for one side. `flask fetch-rates` has never run, or the
        # currency is not one it fetches. Nothing to compare against.
        return

    bigger, smaller = max(left, right), min(left, right)
    if bigger <= smaller * 10:
        return

    raise EntryError(
        f"{format_minor(amount_minor, currency)} {currency} is not worth "
        f"{format_minor(counter_minor, counter_currency)} {counter_currency} — "
        f"that is out by a factor of about {bigger // max(smaller, 1)}. Enter the "
        f"amount that actually landed in the other account.",
        "counter_amount",
    )


def _valid_date(raw: str, user_tz: str) -> str:
    raw = (raw or "").strip()
    if not raw:
        return today_for(user_tz).isoformat()
    try:
        parsed = datetime.strptime(raw, "%Y-%m-%d").date()
    except ValueError:
        raise EntryError("Use a date like 2026-08-15.", "occurred_on") from None

    # A date far in the future is a typo, not an intention. A wide past window
    # stays open because back-filling a month of receipts is a real thing to do.
    if parsed > today_for(user_tz) + timedelta(days=1):
        raise EntryError("That date is in the future.", "occurred_on")
    if parsed < date(2000, 1, 1):
        raise EntryError("That date looks wrong.", "occurred_on")
    return parsed.isoformat()


def _fx_rate(raw: str, currency: str, base: str) -> float | None:
    """The rate on the day of purchase, captured now because it is unrecoverable.

    Stored as REAL, which is the one float in the schema and is fine: it is a
    rate, not money. Converting with it happens in money.convert_to_base(),
    in Decimal, never as float arithmetic inside SQL.
    """
    if currency == base:
        return None
    raw = (raw or "").strip()
    if not raw:
        raise EntryError(f"Enter the {currency}→{base} rate used today.", "fx_rate_to_base")
    try:
        rate = float(raw)
    except ValueError:
        raise EntryError("That rate wasn't understood.", "fx_rate_to_base") from None
    if not (0 < rate < 1_000_000):
        raise EntryError("That rate looks wrong.", "fx_rate_to_base")
    return rate


# ------------------------------------------------------------------- create


def _prepare(user, form, exclude_id: int | None = None) -> dict:
    """Validate a submission and return the columns it becomes.

    The single place the rules live. `create_transaction` and
    `update_transaction` both come through here, so an edit cannot slip past a
    check that an insert has to pass — rule 6's "there is no second path" means
    no second set of rules either, not just no second SQL statement.

    `form` is any mapping — request.form in the app, a plain dict in tests.
    `exclude_id` is the row being replaced, kept out of the period sums.
    """
    base = base_currency()

    direction = (form.get("direction") or "spend").strip()
    if direction not in DIRECTIONS:
        raise EntryError("Pick spend, income or transfer.", "direction")

    # A transfer moves money between two of your own accounts. It has no other
    # side, so the merchant fields are not merely ignored — they are never read.
    merchant_kind = "income" if direction == "income" else "spend"

    merchant_id = _int_or_none(form.get("merchant_id"), "merchant")
    new_merchant = (form.get("new_merchant_name") or "").strip()
    if not merchant_id and new_merchant and direction != "transfer":
        merchant_id = find_or_create_merchant(new_merchant, merchant_kind)

    merchant = None
    if merchant_id is not None and direction != "transfer":
        merchant = query_one(
            "SELECT id, name, default_category_id, default_is_online, default_account_id, kind "
            "FROM merchants WHERE id = ? AND is_active = 1",
            (merchant_id,),
        )
        if merchant is None:
            raise EntryError("That merchant no longer exists.", "merchant")
        if merchant["kind"] not in (merchant_kind, "both"):
            side = "an income source" if merchant["kind"] == "income" else "a merchant you spend at"
            wanted = "income sources" if merchant_kind == "income" else "merchants"
            raise EntryError(
                f"{merchant['name']} is {side}. Pick from the {wanted} list instead.",
                "merchant",
            )

    # Merchant defaults are applied server-side for anything left blank, so the
    # form still fills itself in with JavaScript switched off.
    account_id = _int_or_none(form.get("account_id"), "account")
    if account_id is None and merchant is not None:
        account_id = merchant["default_account_id"]
    if account_id is None:
        first = query_one(
            "SELECT id FROM accounts WHERE is_active = 1 ORDER BY sort_order, name LIMIT 1"
        )
        if first is None:
            raise EntryError("Add an account before logging anything.", "account")
        account_id = first["id"]
    account = _account(account_id, "account")

    category_id = _int_or_none(form.get("category_id"), "category")
    if category_id is None and merchant is not None:
        category_id = merchant["default_category_id"]
    if category_id is not None:
        if query_one("SELECT 1 FROM categories WHERE id = ?", (category_id,)) is None:
            raise EntryError("That category no longer exists.", "category")

    currency = (form.get("currency") or account["currency"] or base).strip().upper()
    if not currency.isalpha() or len(currency) != 3:
        raise EntryError("Pick a currency.", "currency")

    try:
        amount_minor = parse_to_minor(form.get("amount"), currency)
    except MoneyError as exc:
        raise EntryError(str(exc), "amount") from None

    fx_rate = _fx_rate(form.get("fx_rate_to_base"), currency, base)
    occurred_on = _valid_date(form.get("occurred_on"), user["timezone"])

    is_online = 1 if str(form.get("is_online") or "0").strip() in ("1", "on", "true") else 0
    if "is_online" not in form and merchant is not None:
        is_online = int(merchant["default_is_online"])

    # Section 4: new transactions inherit the owner's default, and the form may
    # flip an individual one either way.
    if "is_shared" in form:
        is_shared = 1 if str(form.get("is_shared")).strip() in ("1", "on", "true") else 0
    else:
        is_shared = int(user["default_shared"])

    note = (form.get("note") or "").strip() or None
    if note and len(note) > 500:
        raise EntryError("That note is too long.", "note")

    # Whether there is a paper trail is its own fact, independent of who was
    # paid. A street vendor with no name may hand over a slip; a named shop may
    # not. Before migration 004 these were welded together as one merchant.
    receiptless = 1 if str(form.get("receiptless") or "0").strip() in ("1", "on", "true") else 0

    # A photo and "there was no receipt" cannot both be true, and the two ways of
    # arriving at that contradiction are not treated alike. Attaching a photo
    # clears the flag (receipts.store() does it, and says so), because the photo
    # is evidence and the flag was a claim. Ticking the box on an entry that
    # already has photos is refused, because the claim cannot beat the evidence.
    # The trigger in migration 005 is the backstop; this is the sentence.
    if receiptless and exclude_id is not None:
        photos = query_one(
            "SELECT COUNT(*) AS n FROM attachments WHERE transaction_id = ?", (exclude_id,)
        )["n"]
        if photos:
            raise EntryError(
                f"This entry has {photos} receipt photo{'s' if photos > 1 else ''} attached. "
                f"Remove {'them' if photos > 1 else 'it'} first if there really was no receipt.",
                "receiptless",
            )

    if direction == "spend":
        _check_credit_limit(account, amount_minor, currency, fx_rate, occurred_on, base,
                            exclude_id=exclude_id)

    # Cash is a pocket, not an account with a transfer facility. What leaves it
    # is spending; what enters it is income, a reimbursement, or a withdrawal
    # logged on the card that dispensed the notes.
    if account["type"] == "cash" and direction == "transfer":
        raise EntryError(
            "Cash only records spending, income and reimbursements. To put money into "
            "cash, log a withdrawal on the card it came out of.",
            "direction",
        )

    counter_account_id = counter_amount_minor = counter_currency = None
    if direction == "transfer":
        counter_account_id = _int_or_none(form.get("counter_account_id"), "counter_account")
        counter = _account(counter_account_id, "counter_account")
        if counter["id"] == account["id"]:
            raise EntryError("Pick two different accounts.", "counter_account")

        if counter["type"] == "cash":
            _check_cash_withdrawal(account, amount_minor, currency, occurred_on,
                                   exclude_id=exclude_id)

        counter_currency = counter["currency"]
        raw_counter = (form.get("counter_amount") or "").strip()

        if counter_currency == currency:
            # Same currency: the amount that lands is the amount that left, and
            # asking twice would be friction with no information in it.
            counter_amount_minor = amount_minor
        else:
            if not raw_counter:
                raise EntryError(
                    f"Enter how much {counter_currency} arrived in {counter['name']}.",
                    "counter_amount",
                )
            try:
                counter_amount_minor = parse_to_minor(raw_counter, counter_currency)
            except MoneyError as exc:
                raise EntryError(str(exc), "counter_amount") from None

            # The two sides have to be able to be the same money.
            _check_transfer_rate(amount_minor, currency,
                                 counter_amount_minor, counter_currency)

        # A transfer is movement between your own accounts, not a purchase.
        merchant_id = None
        category_id = category_id if form.get("category_id") else None
        is_online = 0

    return {
        "occurred_on": occurred_on, "direction": direction, "amount_minor": amount_minor,
        "currency": currency, "fx_rate_to_base": fx_rate, "account_id": account["id"],
        "counter_account_id": counter_account_id, "counter_amount_minor": counter_amount_minor,
        "counter_currency": counter_currency, "merchant_id": merchant_id,
        "category_id": category_id, "is_online": is_online, "note": note,
        "is_shared": is_shared, "receiptless": receiptless,
    }


_COLUMNS = (
    "occurred_on", "direction", "amount_minor", "currency", "fx_rate_to_base",
    "account_id", "counter_account_id", "counter_amount_minor", "counter_currency",
    "merchant_id", "category_id", "is_online", "note", "is_shared", "receiptless",
)


def create_transaction(user, form) -> int:
    """Validate and insert. Returns the new transaction id."""
    cols = _prepare(user, form)
    now = utc_now()
    placeholders = ",".join("?" for _ in _COLUMNS)
    cur = execute(
        f"INSERT INTO transactions (user_id, {', '.join(_COLUMNS)}, created_at, updated_at) "
        f"VALUES (?, {placeholders}, ?, ?)",
        (user["id"], *(cols[c] for c in _COLUMNS), now, now),
    )
    return cur.lastrowid


def update_transaction(txn_id: int, user, form) -> None:
    """Validate and replace an existing row.

    The owner never changes: editing someone's transaction — which only admin
    can do — must not quietly reassign whose it is, because `user_id` is what
    the visibility rule keys off. Changing it would move the row out of its
    owner's sight.
    """
    existing = query_one("SELECT * FROM transactions WHERE id = ?", (txn_id,))
    if existing is None:
        raise EntryError("That entry no longer exists.", "")
    if not can_edit(user, existing):
        raise EntryError("That entry belongs to someone else.", "")

    cols = _prepare(user, form, exclude_id=txn_id)
    assignments = ", ".join(f"{c} = ?" for c in _COLUMNS)
    execute(
        f"UPDATE transactions SET {assignments}, updated_at = ? WHERE id = ?",
        (*(cols[c] for c in _COLUMNS), utc_now(), txn_id),
    )


def delete_transaction(txn_id: int, user) -> bool:
    """Remove a transaction. Attachments follow it by ON DELETE CASCADE."""
    existing = query_one("SELECT id, user_id FROM transactions WHERE id = ?", (txn_id,))
    if existing is None or not can_edit(user, existing):
        return False
    execute("DELETE FROM transactions WHERE id = ?", (txn_id,))
    return True


# --------------------------------------------------------------------- undo


def undoable(txn_id: int, user):
    """The transaction the toast's Undo would remove, or None.

    Only the owner, only inside the window. Admin is not special here: Undo is
    for the person who just pressed Save, and a wider rule would make it a
    deletion tool wearing a friendlier label.
    """
    row = query_one(
        "SELECT id, user_id, amount_minor, currency, created_at, merchant_id "
        "FROM transactions WHERE id = ?",
        (txn_id,),
    )
    if row is None or row["user_id"] != user["id"]:
        return None
    try:
        created = datetime.strptime(row["created_at"], "%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        return None
    # created_at is naive UTC text; compare against naive UTC, not local time.
    if datetime.now(timezone.utc).replace(tzinfo=None) - created > UNDO_WINDOW:
        return None
    return row


def undo(txn_id: int, user) -> bool:
    row = undoable(txn_id, user)
    if row is None:
        return False
    execute("DELETE FROM transactions WHERE id = ?", (txn_id,))
    return True


def describe(txn_id: int) -> dict:
    """Amount and merchant for the confirmation toast."""
    row = query_one(
        "SELECT t.amount_minor, t.currency, t.direction, "
        "       COALESCE(m.name, a.name) AS what "
        "FROM transactions t "
        "LEFT JOIN merchants m ON m.id = t.merchant_id "
        "LEFT JOIN accounts  a ON a.id = t.account_id "
        "WHERE t.id = ?",
        (txn_id,),
    )
    return dict(row) if row else {}
