"""Budgets: what the household meant to spend, against what it actually did.

**A budget never blocks anything.** It is not a rule, it is an intention, and an
app that refuses to record a purchase because the purchase was unwise has
stopped being a record of what happened. The spec says warn only, and everything
here returns numbers — nothing in this module raises at a writer.

These are *not* the limits on `accounts`. `credit_limit_local_minor` and
`withdrawal_limit_minor` are ceilings a bank set, they are real, and
`transactions.py` does refuse a save that would cross one. The two ideas share
an English word and nothing else.

## The visibility exception, stated

Rule 4 says every transaction read goes through `visibility_sql()`, and names
`balances.py` as the one place that does not. This is the second, and it needs
saying out loud rather than inheriting the first one's excuse.

A budget total is an aggregate over everyone's spending. A household budget
filtered to what the reader personally may see is not a partial answer — it is a
*wrong* one, a number that says the family has 3,000 left when it has 200, which
is worse than showing nothing. So the arithmetic below reads unfiltered, exactly
as balances do, and for the same reason: aggregates over the household are the
household's own business, individual transactions never are.

What is filtered is *which budgets a person can see at all*, in
`visible_limits()`. A budget about one family member is that member's and
admin's; a budget about the household, a category, an account or a shop is
everybody's. That is the line, and it is the honest one — a member should not
learn from a progress bar that someone else is 90% through their personal
allowance.

The cron sweep has no reader at all, which is the other half of why the filter
could not have been used here: `visibility_sql(None)` fails closed and would
make every budget read as zero spent, forever, silently.
"""

from __future__ import annotations

from datetime import date, timedelta

from db import base_currency, execute, query, query_one, utc_now
from money import convert_to_base

SCOPES = ("household", "user", "category", "account", "merchant")
PERIODS = ("weekly", "monthly")

# Alerts fire at the budget's own warning mark and again when it is spent. Two
# messages per budget per period is the most this will ever send, and the UNIQUE
# on limit_alerts is what makes that a guarantee rather than an intention.
FULL_PCT = 100


# ------------------------------------------------------------------- periods


def period_key(period: str, day: date) -> str:
    """The string that identifies this budget's current window.

    Monthly is '2026-08'. Weekly is ISO — '2026-W33', weeks starting Monday —
    which is what `date.isocalendar()` gives and what a calendar on a phone
    shows. The alternative, a week that starts on the day the budget was
    created, produces windows nobody can find the edges of.
    """
    if period == "weekly":
        year, week, _ = day.isocalendar()
        return f"{year}-W{week:02d}"
    return day.strftime("%Y-%m")


def period_bounds(period: str, day: date) -> tuple[str, str]:
    """[start, end) as ISO dates — half-open, so no row lands in two windows."""
    if period == "weekly":
        start = day - timedelta(days=day.weekday())
        return start.isoformat(), (start + timedelta(days=7)).isoformat()

    first = day.replace(day=1)
    nxt = (first.replace(year=first.year + 1, month=1) if first.month == 12
           else first.replace(month=first.month + 1))
    return first.isoformat(), nxt.isoformat()


def period_label(period: str, day: date) -> str:
    if period == "weekly":
        start, _ = period_bounds(period, day)
        monday = date.fromisoformat(start)
        # Built by hand rather than with strftime: '%-d' is not portable and
        # this project is developed on Windows and deployed on a Pi.
        return f"week of {monday.day} {monday.strftime('%B')}"
    return day.strftime("%B %Y")


# -------------------------------------------------------------------- scopes


def _scope_where(row) -> tuple[str, list]:
    """The SQL that says which transactions this budget is about."""
    scope_id = row["scope_id"]

    if row["scope_type"] == "user":
        return "t.user_id = ?", [scope_id]

    if row["scope_type"] == "category":
        # A budget on "Eating Out" means the family it heads. Anything else
        # would make organising your categories quietly shrink your budgets.
        return (
            "(t.category_id = ? OR t.category_id IN "
            "  (SELECT id FROM categories WHERE parent_id = ?))",
            [scope_id, scope_id],
        )

    if row["scope_type"] == "account":
        # Money spent on a debit card comes out of the bank behind it, so a
        # budget on the bank counts its cards too — the same settlement rule
        # balances.py resolves before it adds anything up.
        settles_to = query_one(
            "SELECT COALESCE(parent_account_id, id) AS s FROM accounts WHERE id = ?",
            (scope_id,),
        )
        target = settles_to["s"] if settles_to else scope_id
        return (
            "t.account_id IN (SELECT id FROM accounts "
            " WHERE COALESCE(parent_account_id, id) = ?)",
            [target],
        )

    if row["scope_type"] == "merchant":
        return "t.merchant_id = ?", [scope_id]

    return "1 = 1", []


def scope_name(row) -> str:
    """What this budget is about, in words, for a screen or a message."""
    table = {"user": "users", "category": "categories",
             "account": "accounts", "merchant": "merchants"}.get(row["scope_type"])
    if table is None:
        return "the household"

    column = "display_name" if table == "users" else "name"
    found = query_one(f"SELECT {column} AS label FROM {table} WHERE id = ?", (row["scope_id"],))
    # A category deleted out from under a budget leaves the budget measuring
    # nothing; saying so beats printing "None".
    return found["label"] if found else "something that no longer exists"


# ---------------------------------------------------------------- evaluation


def spent(row, day: date) -> tuple[int, int]:
    """(spent this period in base currency, rows that could not be converted).

    Unfiltered by design — see the module docstring. Conversion happens here in
    Python through `money.convert_to_base()`; `SUM(amount_minor * fx_rate)` in
    SQL is float arithmetic on money, which rule 1 exists to forbid.
    """
    start, end = period_bounds(row["period"], day)
    scope_sql, params = _scope_where(row)
    base = base_currency()

    rows = query(
        f"SELECT t.amount_minor, t.currency, t.fx_rate_to_base FROM transactions t "
        f"WHERE t.direction = 'spend' AND t.occurred_on >= ? AND t.occurred_on < ? "
        f"  AND {scope_sql}",
        [start, end, *params],
    )

    total = unconverted = 0
    for txn in rows:
        if txn["currency"] == base:
            total += txn["amount_minor"]
        elif txn["fx_rate_to_base"]:
            total += convert_to_base(txn["amount_minor"], txn["fx_rate_to_base"])
        else:
            unconverted += 1
    return total, unconverted


def evaluate(row, day: date) -> dict:
    """Everything a progress bar or a warning needs about one budget."""
    used, unconverted = spent(row, day)
    amount = row["amount_minor"]
    # Integer percentage, and the bar is capped at 100 while the number is not:
    # a bar that overflows its track is a rendering bug, but "142% of August"
    # is the fact worth reading.
    pct = (used * 100 // amount) if amount else 0

    return {
        "limit": row,
        "name": row["name"],
        "scope": scope_name(row),
        "period_key": period_key(row["period"], day),
        "period_label": period_label(row["period"], day),
        "spent_minor": used,
        "amount_minor": amount,
        "remaining_minor": amount - used,
        "pct": pct,
        "bar_pct": min(pct, 100),
        "over": used > amount,
        "warning": pct >= row["warn_pct"],
        # Same honesty as an approximate balance: a budget that silently omits a
        # foreign charge is a budget that reads low on exactly the month someone
        # travelled.
        "unconverted": unconverted,
    }


# -------------------------------------------------------------------- access


def visible_limits(user) -> list:
    """The budgets this person may see at all. See the module docstring.

    Admin sees every budget. A member sees the household's — which includes the
    ones scoped to a category, an account or a shop, because those are shared
    facts — plus any budget that is about them personally, and no other person's.
    """
    if user is None:
        return []
    if user["role"] == "admin":
        return query(
            "SELECT * FROM limits WHERE is_active = 1 ORDER BY scope_type, name")
    return query(
        "SELECT * FROM limits WHERE is_active = 1 "
        "  AND (scope_type <> 'user' OR scope_id = ?) ORDER BY scope_type, name",
        (user["id"],),
    )


def dashboard_limits(user, day: date) -> list[dict]:
    """Evaluated budgets for the month screen, the ones closest to the edge first."""
    return sorted(
        (evaluate(row, day) for row in visible_limits(user)),
        key=lambda item: item["pct"],
        reverse=True,
    )


# --------------------------------------------------------------------- alerts


def _recipients(row) -> list[dict]:
    """Who hears about this budget.

    A budget about one person messages that person, and admin as well, because
    admin is who set it and who will be asked about it. Every other kind of
    budget is a household fact with no single subject, so it goes to admin only
    — a category budget is not news anyone else asked for.

    Anyone without a chat id is simply not in the list. Telegram will not let a
    bot write first, so a missing id means that person has not started the
    conversation, which is not a failure this can fix.
    """
    people = query(
        "SELECT id, display_name, telegram_chat_id, role FROM users "
        "WHERE is_active = 1 AND telegram_chat_id IS NOT NULL AND telegram_chat_id <> ''"
    )
    wanted = [p for p in people if p["role"] == "admin"]
    if row["scope_type"] == "user":
        wanted += [p for p in people if p["id"] == row["scope_id"]]

    seen: dict[str, dict] = {}
    for person in wanted:
        seen.setdefault(person["telegram_chat_id"], dict(person))
    return list(seen.values())


def due_thresholds(row, state: dict) -> list[int]:
    """Which marks this budget has crossed and not yet spoken about.

    Both are checked every run, so a budget that jumped straight past its warning
    mark to 140% in one purchase still sends the warning — the two messages read
    as a story rather than as one arriving out of nowhere.
    """
    marks = sorted({row["warn_pct"], FULL_PCT})
    crossed = [mark for mark in marks if state["pct"] >= mark]
    if not crossed:
        return []

    already = {
        alert["threshold_pct"]
        for alert in query(
            "SELECT threshold_pct FROM limit_alerts WHERE limit_id = ? AND period_key = ?",
            (row["id"], state["period_key"]),
        )
    }
    return [mark for mark in crossed if mark not in already]


def message(state: dict, threshold: int, currency: str) -> str:
    """The text that lands on someone's phone. Plain sentences, no jargon."""
    from money import format_minor

    spent_text = f"{format_minor(state['spent_minor'], currency)} {currency}"
    budget_text = f"{format_minor(state['amount_minor'], currency)} {currency}"
    head = f"{state['name']} — {state['period_label']}"

    if threshold >= FULL_PCT:
        over = format_minor(abs(state["remaining_minor"]), currency)
        body = (f"Spent {spent_text} of {budget_text}. That is the whole budget"
                + (f", and {over} {currency} past it." if state["over"] else "."))
    else:
        left = format_minor(state["remaining_minor"], currency)
        body = (f"Spent {spent_text} of {budget_text} — {state['pct']}%. "
                f"{left} {currency} left.")

    if state["unconverted"]:
        body += (f"\n\n{state['unconverted']} purchase(s) in another currency are not "
                 f"counted — they were logged without an exchange rate.")
    return f"{head}\n{body}"


def record(limit_id: int, period_key_value: str, threshold: int, spent_minor: int) -> None:
    """Remember that this was said, so it is never said twice.

    Written only after a message actually goes out. Recording first and failing
    to send would mean a warning that is permanently owed and never delivered;
    this way a bad token or a flat network costs a retry on the next cron run.
    """
    execute(
        "INSERT OR IGNORE INTO limit_alerts (limit_id, period_key, threshold_pct, "
        "spent_minor, sent_at) VALUES (?,?,?,?,?)",
        (limit_id, period_key_value, threshold, spent_minor, utc_now()),
    )


def sweep(day: date, token: str, dry_run: bool = False) -> list[str]:
    """Check every active budget and send what is owed. Returns lines to log.

    Called only by `flask check-limits`, which is a cron command. Nothing in
    `blueprints/` imports this function, and `verify_limits.py` checks that it
    stays true — a budget sweep that fires from a page view would put
    api.telegram.org between someone and their entry form.
    """
    # Imported here rather than at the top of the file so that importing
    # `limits` — which the dashboard does, on every page view — never pulls in
    # the module that opens sockets.
    import telegram

    currency = base_currency()
    lines: list[str] = []

    for row in query("SELECT * FROM limits WHERE is_active = 1 ORDER BY id"):
        if row["currency"] != currency:
            # The base currency changed after this budget was written, so the
            # ceiling and the total are in different units. Same call the
            # international card limit makes: say so, check nothing.
            lines.append(
                f"{row['name']}: set in {row['currency']}, household now counts in "
                f"{currency} — skipped. Edit the budget to re-set it.")
            continue

        state = evaluate(row, day)
        for threshold in due_thresholds(row, state):
            people = _recipients(row)
            text = message(state, threshold, currency)

            if dry_run:
                who = ", ".join(p["display_name"] for p in people) or "nobody (no chat ids set)"
                lines.append(f"would send to {who}: {text.splitlines()[0]} at {threshold}%")
                continue

            if not people:
                # Nothing recorded, so the warning is still owed. The moment
                # somebody pastes in a chat id, the next run delivers it.
                lines.append(
                    f"{row['name']} is at {state['pct']}% but nobody has a Telegram chat "
                    f"id — see `flask telegram-chats`.")
                continue

            sent = 0
            for person in people:
                try:
                    telegram.send(token, person["telegram_chat_id"], text)
                    sent += 1
                except Exception as exc:
                    lines.append(f"{row['name']} → {person['display_name']}: {exc}")

            if sent:
                record(row["id"], state["period_key"], threshold, state["spent_minor"])
                lines.append(
                    f"{row['name']} at {state['pct']}% — told {sent} "
                    f"{'person' if sent == 1 else 'people'}")

    if not lines:
        lines.append("every budget is inside its warning mark — nothing to send")
    return lines
