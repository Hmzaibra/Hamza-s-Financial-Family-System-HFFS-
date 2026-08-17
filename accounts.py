"""Who an account belongs to, and the per-account screens that follow from it.

Ownership is **not** visibility. Section 4 keys off a transaction's owner and
never off its account, because a shared bank account must not expose one
person's spending to everyone else who draws on it. What ownership decides is
whose "My accounts" list an account appears in, and nothing else. Adding
somebody to an account lets them see the account. It shows them no purchases
they could not already see.

Admin sees every account, always. That is not a special case bolted on: an
admin already sees every transaction, so hiding the container while showing the
contents would be a lock on a door with no wall.

The balance arithmetic here is `balances.py`'s, not a second copy. What this
module adds is the *sequence* — what the balance was after each entry, walking
backwards from what it is now — which is a different question from what it is,
and the one a statement answers.
"""

from __future__ import annotations

from db import base_currency, execute, query, query_one, utc_now
from money import convert_to_base, format_minor

import balances as bal

ADMIN = "admin"


# ------------------------------------------------------------------ ownership


def owners_of(account_id: int) -> list:
    return query(
        "SELECT u.id, u.display_name FROM account_owners o "
        "JOIN users u ON u.id = o.user_id WHERE o.account_id = ? "
        "ORDER BY u.display_name",
        (account_id,),
    )


def owner_names() -> dict[int, list[str]]:
    """account id → the names on it, for a list that draws many rows at once."""
    out: dict[int, list[str]] = {}
    for row in query(
        "SELECT o.account_id, u.display_name FROM account_owners o "
        "JOIN users u ON u.id = o.user_id ORDER BY u.display_name"
    ):
        out.setdefault(row["account_id"], []).append(row["display_name"])
    return out


def owner_id_map() -> dict[int, list[int]]:
    """account id → the user ids on it, for a form that draws many at once."""
    out: dict[int, list[int]] = {}
    for row in query("SELECT account_id, user_id FROM account_owners ORDER BY user_id"):
        out.setdefault(row["account_id"], []).append(row["user_id"])
    return out


def owner_ids(account_id: int) -> set[int]:
    return {row["user_id"] for row in query(
        "SELECT user_id FROM account_owners WHERE account_id = ?", (account_id,))}


def set_owners(account_id: int, user_ids) -> None:
    """Replace the set of owners. Empty means the household's, which is a state.

    Delete-then-insert rather than a diff: the set is three or four rows, the
    whole thing is one statement each way, and a diff would be more code to get
    subtly wrong for no measurable gain.
    """
    wanted = sorted({int(uid) for uid in user_ids if str(uid).strip()})
    execute("DELETE FROM account_owners WHERE account_id = ?", (account_id,))
    if not wanted:
        return
    now = utc_now()
    for uid in wanted:
        execute(
            "INSERT OR IGNORE INTO account_owners (account_id, user_id, created_at) "
            "VALUES (?,?,?)",
            (account_id, uid, now),
        )


def visible_accounts(user) -> list:
    """The accounts this person's "My accounts" screen shows.

    Admin gets everything. Everyone else gets what they are named on, plus the
    cards and Instapay handles hanging off those — a card is a way of reaching an
    account you already own, so hiding it while showing its parent would be
    arbitrary. Accounts nobody has claimed are the household's and are shown to
    everyone; that is what an empty owner set has meant since 006.
    """
    if user is None:
        return []

    if user["role"] == ADMIN:
        return query(
            "SELECT * FROM accounts WHERE is_active = 1 ORDER BY sort_order, name")

    return query(
        "SELECT * FROM accounts a WHERE a.is_active = 1 AND ("
        # named on it
        "  a.id IN (SELECT account_id FROM account_owners WHERE user_id = ?)"
        # or on the account it draws from
        "  OR a.parent_account_id IN "
        "     (SELECT account_id FROM account_owners WHERE user_id = ?)"
        # or nobody has claimed it, and nobody has claimed its parent either
        "  OR (NOT EXISTS (SELECT 1 FROM account_owners WHERE account_id = a.id)"
        "      AND (a.parent_account_id IS NULL OR NOT EXISTS "
        "           (SELECT 1 FROM account_owners WHERE account_id = a.parent_account_id)))"
        ") ORDER BY a.sort_order, a.name",
        (user["id"], user["id"]),
    )


def may_see(user, account) -> bool:
    """Whether this person's account screens may open this account."""
    if user is None or account is None:
        return False
    return account["id"] in {row["id"] for row in visible_accounts(user)}


# --------------------------------------------------------------- the summary


def settles_to(account_id: int) -> int:
    """The account whose balance this one actually moves."""
    row = query_one(
        "SELECT COALESCE(parent_account_id, id) AS s FROM accounts WHERE id = ?",
        (account_id,),
    )
    return row["s"] if row else account_id


def _leg_ids(settlement_id: int) -> list[int]:
    """Every account whose movements land on this settlement account."""
    return [row["id"] for row in query(
        "SELECT id FROM accounts WHERE COALESCE(parent_account_id, id) = ?",
        (settlement_id,))]


def month_here(account_id: int, day, vis_sql: str, vis_params: list) -> dict:
    """What this account did this month: spent, received, and the top categories.

    An ordinary transaction read, so it goes through the visibility filter like
    every other list of purchases. Only the *balance* is exempt — see
    `balances.py` — and the two sit side by side on the summary screen precisely
    because they answer different questions and are allowed different rules.
    """
    start, end = bal.month_bounds(day)
    base = base_currency()
    legs = _leg_ids(settles_to(account_id))
    marks = ",".join("?" for _ in legs)

    rows = query(
        f"SELECT t.direction, t.amount_minor, t.currency, t.fx_rate_to_base, "
        f"       COALESCE(p.name, c.name, 'Uncategorised') AS group_name, "
        f"       COALESCE(p.icon, c.icon) AS icon "
        f"  FROM transactions t "
        f"  LEFT JOIN categories c ON c.id = t.category_id "
        f"  LEFT JOIN categories p ON p.id = c.parent_id "
        f" WHERE {vis_sql} AND t.account_id IN ({marks}) "
        f"   AND t.occurred_on >= ? AND t.occurred_on < ?",
        [*vis_params, *legs, start, end],
    )

    spent = received = unconverted = 0
    buckets: dict[str, dict] = {}
    for row in rows:
        if row["currency"] == base:
            amount = row["amount_minor"]
        elif row["fx_rate_to_base"]:
            amount = convert_to_base(row["amount_minor"], row["fx_rate_to_base"])
        else:
            unconverted += 1
            continue

        if row["direction"] == "income":
            received += amount
            continue
        if row["direction"] != "spend":
            # A transfer out is not spending. It is the same money, somewhere
            # else, and counting it here would double it against the account it
            # landed in.
            continue

        spent += amount
        bucket = buckets.setdefault(
            row["group_name"], {"name": row["group_name"], "icon": row["icon"],
                                "minor": 0, "count": 0})
        bucket["minor"] += amount
        bucket["count"] += 1

    ranked = sorted(buckets.values(), key=lambda b: b["minor"], reverse=True)
    top = ranked[:5]
    biggest = top[0]["minor"] if top else 0
    for bucket in top:
        bucket["share"] = (bucket["minor"] * 100 // biggest) if biggest else 0

    return {"spent": spent, "received": received, "unconverted": unconverted,
            # The five for the summary card, and all of them for the month
            # comparison — which needs the categories that went *down* as much
            # as the ones that went up, and those are rarely in a top five.
            "top": top, "by_category": ranked, "currency": base}


def reconcile(account) -> dict:
    """Every movement that made this balance what it is, opening to today.

    This exists because the summary screen showed "spent 50, came in 6,040" next
    to a balance of 5,387 and left a reader to work out the 603 for themselves —
    which they cannot, because the two lines that close the gap were not on the
    screen at all. An opening balance of −3 and a 600 transfer out are both real
    and both invisible, and three numbers that refuse to add up are worse than
    one number on its own.

    Two windows were being compared as well. The month card is *this month*; the
    balance is *all time*. This is all time, so it lands exactly on the balance
    and can be read as a sum rather than as a coincidence.

    Transfers get their own two lines rather than being folded into either side.
    Moving your own money is neither spending nor income — the whole reason it is
    excluded from both figures — but it is unarguably why the balance moved.
    """
    settlement = settles_to(account["id"])
    legs = set(_leg_ids(settlement))
    marks = ",".join("?" for _ in legs)
    base = base_currency()

    settlement_row = query_one(
        "SELECT id, name, currency, "
        "       (SELECT COALESCE(SUM(opening_balance_minor), 0) FROM accounts "
        "         WHERE COALESCE(parent_account_id, id) = ?) AS opening "
        "  FROM accounts WHERE id = ?",
        (settlement, settlement),
    )
    code = settlement_row["currency"]

    rows = query(
        f"SELECT * FROM transactions t "
        f" WHERE t.account_id IN ({marks}) OR t.counter_account_id IN ({marks})",
        [*legs, *legs],
    )

    lines = {"income": 0, "spend": 0, "transfer_out": 0, "transfer_in": 0}
    unconverted = 0

    for row in rows:
        # Deliberately the same conversion `_effect()` makes, so a leg this can
        # express and the balance cannot — or the reverse — is impossible.
        def amount(minor, currency):
            nonlocal unconverted
            if currency == code:
                return minor
            if code == base and row["fx_rate_to_base"]:
                return convert_to_base(minor, row["fx_rate_to_base"])
            unconverted += 1
            return 0

        if row["account_id"] in legs:
            if row["direction"] == "income":
                lines["income"] += amount(row["amount_minor"], row["currency"])
            elif row["direction"] == "spend":
                lines["spend"] += amount(row["amount_minor"], row["currency"])
            else:
                lines["transfer_out"] += amount(row["amount_minor"], row["currency"])

        if row["direction"] == "transfer" and row["counter_account_id"] in legs:
            lines["transfer_in"] += amount(
                row["counter_amount_minor"], row["counter_currency"])

    total = (settlement_row["opening"] + lines["income"] - lines["spend"]
             - lines["transfer_out"] + lines["transfer_in"])

    standing = bal.account_balances().get(settlement)

    return {
        "settlement": settlement_row,
        "currency": code,
        "opening": settlement_row["opening"],
        **lines,
        "total": total,
        "unconverted": unconverted,
        # The column is only worth printing as a sum if it *is* the balance.
        # If this is ever False the screen has a bug, and the template says so
        # rather than showing a total that quietly disagrees with the one at the
        # top of the same page.
        "agrees": standing is None or total == standing.minor,
    }


def previous_month(day):
    """The same day-of-month a month earlier, clamped to a day that exists.

    Only ever used to pick a month, never as a date in its own right — the 28th
    is enough to name February. Clamping rather than subtracting 30 days, which
    would land in the wrong month twice a year.
    """
    first = day.replace(day=1)
    return (first.replace(year=first.year - 1, month=12) if first.month == 1
            else first.replace(month=first.month - 1))


def month_compare(account_id: int, day, vis_sql: str, vis_params: list) -> dict:
    """This month against the one before it, for one account.

    Both months come from `month_here()` — the same function, called twice with
    a day in each — rather than a second query that could drift from it. The
    figure on this card and the figure on the one above it are then the same
    number by construction, not by review.

    The direction of "good" is inverted here and nowhere else in the app.
    Spending *more* than last month is the thing worth noticing, so an increase
    is the alarming colour, which is the opposite of the +/− convention on an
    amount. The template says so where it paints it.
    """
    before = previous_month(day)
    now = month_here(account_id, day, vis_sql, vis_params)
    then = month_here(account_id, before, vis_sql, vis_params)

    def line(name, icon, this_minor, last_minor):
        return {
            "name": name, "icon": icon,
            "this": this_minor, "last": last_minor,
            "delta": this_minor - last_minor,
            # Percentages need a denominator. A category that did not exist last
            # month is not "up 100%", it is new, and saying so is more useful
            # than a number that happens to be computable.
            "pct": ((this_minor - last_minor) * 100 // last_minor) if last_minor else None,
            "is_new": last_minor == 0 and this_minor > 0,
            "is_gone": this_minor == 0 and last_minor > 0,
        }

    # Every category either month touched, so one that stopped entirely is still
    # a row. A breakdown that only lists what you spent on hides the good news.
    names = {b["name"]: b["icon"] for b in now["by_category"]}
    names.update({b["name"]: b["icon"] for b in then["by_category"]})
    this_by = {b["name"]: b["minor"] for b in now["by_category"]}
    last_by = {b["name"]: b["minor"] for b in then["by_category"]}

    rows = [line(name, icon, this_by.get(name, 0), last_by.get(name, 0))
            for name, icon in names.items()]
    # Biggest movement first, in either direction — the point of the card is
    # what changed, not what is large.
    rows.sort(key=lambda r: abs(r["delta"]), reverse=True)

    return {
        "currency": now["currency"],
        "this_label": day.strftime("%B"),
        "last_label": before.strftime("%B"),
        "spent": line("Spent", None, now["spent"], then["spent"]),
        "received": line("Came in", None, now["received"], then["received"]),
        "rows": rows,
        "unconverted": now["unconverted"] + then["unconverted"],
    }


def wheels(account, day) -> list[dict]:
    """The ceilings on this account, as fractions of themselves.

    Two unrelated kinds of ceiling share this list, and the label on each says
    which is which, because they behave completely differently:

      * the card's own limits, set by the bank, which `transactions.py` refuses
        a save over
      * budgets scoped to this account, which only ever warn

    Both are drawn the same way because both answer "how much of this is left",
    and neither is any use as a number without its denominator.
    """
    import limits as budgets

    out: list[dict] = []
    code = account["currency"]

    if account["type"] in ("credit_card", "debit_card") and account["withdrawal_limit_minor"]:
        taken = query_one(
            "SELECT COALESCE(SUM(t.amount_minor), 0) AS n FROM transactions t "
            "JOIN accounts a ON a.id = t.counter_account_id "
            "WHERE t.account_id = ? AND t.direction = 'transfer' AND a.type = 'cash' "
            "  AND t.occurred_on = ? AND t.currency = ?",
            (account["id"], day.isoformat(), code),
        )["n"]
        out.append(_wheel("Cash today", taken, account["withdrawal_limit_minor"], code,
                          "the bank's daily ATM ceiling — a save over it is refused"))

    if account["type"] == "credit_card":
        month = day.strftime("%Y-%m")
        for column, label, same in (
            ("credit_limit_local_minor", "Local this month", True),
            ("credit_limit_intl_minor", "International this month", False),
        ):
            ceiling = account[column]
            if not ceiling:
                continue
            rows = query(
                f"SELECT amount_minor, currency, fx_rate_to_base FROM transactions "
                f"WHERE account_id = ? AND direction = 'spend' AND occurred_on LIKE ? "
                f"  AND currency {'=' if same else '<>'} ?",
                (account["id"], f"{month}-%", code),
            )
            used = sum(
                row["amount_minor"] if same
                else convert_to_base(row["amount_minor"], row["fx_rate_to_base"])
                for row in rows
            )
            out.append(_wheel(label, used, ceiling, code,
                              "the bank's monthly ceiling — a save over it is refused"))

    for row in query(
        "SELECT * FROM limits WHERE is_active = 1 AND scope_type = 'account' AND scope_id = ?",
        (account["id"],),
    ):
        state = budgets.evaluate(row, day)
        out.append(_wheel(row["name"], state["spent_minor"], state["amount_minor"],
                          row["currency"], "a budget — this one only warns"))

    return out


# The wheel's geometry. A ring is drawn as a circle with a dashed stroke whose
# first dash is the filled arc, which means the only number the template needs is
# a length — and `stroke-dasharray` is an SVG presentation *attribute*, so it
# survives a CSP with no `unsafe-inline` where an inline style would not (rule 8,
# the same reason card colours are painted as `fill`).
WHEEL_RADIUS = 26
WHEEL_CIRCUMFERENCE = 163.4          # 2 * pi * 26, to one decimal


def _wheel(label: str, used: int, ceiling: int, currency: str, note: str) -> dict:
    pct = (used * 100 // ceiling) if ceiling else 0
    # Capped while the number is not, for the same reason the budget bars are:
    # 142% is the fact, a ring past its own circumference is a drawing mistake.
    ring = min(pct, 100)
    return {
        "label": label, "used": used, "ceiling": ceiling, "currency": currency,
        "note": note, "pct": pct, "ring": ring,
        "left": max(0, ceiling - used),
        "over": used > ceiling,
        "radius": WHEEL_RADIUS,
        # A length in user units, not money — floats are fine here for the same
        # reason `fx_rate_to_base` is one.
        "dash": f"{WHEEL_CIRCUMFERENCE * ring / 100:.1f} {WHEEL_CIRCUMFERENCE:.1f}",
    }


# --------------------------------------------------------- balance history


def history(account, user, vis_sql: str, vis_params: list, limit: int = 20) -> dict:
    """The balance after each entry, newest first.

    Walked backwards from the balance the account has *now*, because that is the
    only number known to be right: a forward walk from the opening balance would
    accumulate every rounding decision and every unconvertible leg, and disagree
    with the figure on the accounts screen by the time it reached the top.

    So each row shows the balance the account stood at once that entry had
    happened, and the next row back is that minus the entry's own effect.

    **Rows you may not see still move the balance.** This is where section 4 and
    the balance exception meet head on, and neither can win outright: filtering
    the arithmetic would print a wrong balance, and unfiltering the list would
    hand you somebody's private purchases. So the arithmetic runs over
    everything and the list shows only what you may see, with a plain marker
    where entries were skipped. The step in the number is already visible; the
    marker only stops it looking like a bug.
    """
    settlement = settles_to(account["id"])
    legs = _leg_ids(settlement)
    marks = ",".join("?" for _ in legs)
    base = base_currency()

    settlement_row = query_one(
        "SELECT id, name, currency FROM accounts WHERE id = ?", (settlement,))
    code = settlement_row["currency"]

    # Every entry that touches this settlement account, from either end,
    # unfiltered — this is balance arithmetic (the stated exception to rule 4).
    rows = query(
        f"SELECT t.*, m.name AS merchant_name, "
        f"       COALESCE(p.name, c.name) AS category_name, "
        f"       a.name AS account_name, ca.name AS counter_account_name "
        f"  FROM transactions t "
        f"  LEFT JOIN merchants  m  ON m.id  = t.merchant_id "
        f"  LEFT JOIN categories c  ON c.id  = t.category_id "
        f"  LEFT JOIN categories p  ON p.id  = c.parent_id "
        f"  LEFT JOIN accounts   a  ON a.id  = t.account_id "
        f"  LEFT JOIN accounts   ca ON ca.id = t.counter_account_id "
        f" WHERE t.account_id IN ({marks}) OR t.counter_account_id IN ({marks}) "
        f" ORDER BY t.occurred_on DESC, t.id DESC",
        [*legs, *legs],
    )

    running = bal.account_balances().get(settlement)
    balance_now = running.minor if running else 0
    approximate = bool(running and running.approximate)

    leg_set = set(legs)
    visible_ids = {
        row["id"] for row in query(
            f"SELECT t.id FROM transactions t WHERE ({vis_sql}) "
            f"  AND (t.account_id IN ({marks}) OR t.counter_account_id IN ({marks}))",
            [*vis_params, *legs, *legs],
        )
    }

    entries: list[dict] = []
    balance = balance_now
    hidden_run = 0

    for row in rows:
        delta, exact = _effect(row, leg_set, code, base)

        if row["id"] in visible_ids:
            entries.append({
                "row": row, "balance_after": balance, "delta": delta,
                "exact": exact, "hidden_before": hidden_run,
            })
            hidden_run = 0
        else:
            hidden_run += 1

        balance -= delta

    window = entries[:limit]
    return {
        "settlement": settlement_row,
        "currency": code,
        "balance_now": balance_now,
        "approximate": approximate,
        "entries": window,
        "series": _series(window, code),
        "total": len(entries),
        "more": max(0, len(entries) - limit),
        # Entries older than the window that nobody may see would otherwise
        # vanish without trace; the count below the last row says so.
        "hidden_after": sum(1 for row in rows if row["id"] not in visible_ids),
    }


# The chart's coordinate space. Fixed and unitless — the SVG is scaled by CSS to
# whatever the screen gives it, so nothing here has to know about pixels.
CHART_W = 300
CHART_H = 90
CHART_PAD = 6


def _series(window: list[dict], code: str) -> dict:
    """The same entries as a left-to-right line, oldest first.

    The list reads newest-first, because "what did I just spend" is the question
    you have at the top of a page. A graph reads the other way, because time
    goes left to right and always has. Same numbers, reversed once, here — so
    the two can never drift apart by being built from different queries.

    Points are laid out in a fixed unitless box that CSS stretches. Everything
    the browser needs is computed here rather than in the script, so the chart
    is fully drawn in the HTML and the slider only ever *moves a marker along
    it* — which is what keeps the screen honest with JavaScript off.
    """
    oldest_first = list(reversed(window))
    if not oldest_first:
        return {"points": [], "path": "", "area": "", "count": 0}

    # The balance *before* the oldest entry shown, so the line starts where the
    # window starts rather than at its first movement.
    start = oldest_first[0]["balance_after"] - oldest_first[0]["delta"]
    values = [start] + [e["balance_after"] for e in oldest_first]

    low, high = min(values), max(values)
    span = high - low
    if span == 0:
        # A flat line belongs in the middle, not welded to an edge.
        low, high, span = low - 1, high + 1, 2

    inner_h = CHART_H - 2 * CHART_PAD
    inner_w = CHART_W - 2 * CHART_PAD
    step = inner_w / max(1, len(values) - 1)

    def y_of(value: int) -> float:
        # SVG y grows downward; money grows upward.
        return round(CHART_PAD + inner_h - (value - low) * inner_h / span, 2)

    coords = [(round(CHART_PAD + i * step, 2), y_of(v)) for i, v in enumerate(values)]

    points = []
    for i, entry in enumerate(oldest_first):
        x, y = coords[i + 1]
        row = entry["row"]
        points.append({
            "x": x, "y": y,
            "balance": entry["balance_after"],
            "delta": entry["delta"],
            # Formatted here, not in the browser. money.py owns the exponent
            # table and every rounding decision in this app, and JavaScript has
            # one number type — re-implementing it there is how a screen starts
            # disagreeing with the database it is reading.
            "balance_text": format_minor(entry["balance_after"], code),
            "delta_text": ("+" if entry["delta"] > 0 else
                           "−" if entry["delta"] < 0 else "")
                          + format_minor(abs(entry["delta"]), code),
            "date": row["occurred_on"],
            "label": (f"{row['account_name']} → {row['counter_account_name']}"
                      if row["direction"] == "transfer"
                      else (row["merchant_name"] or row["category_name"]
                            or row["account_name"] or "Entry")),
            "id": row["id"],
        })

    line = " ".join(f"{x},{y}" for x, y in coords)
    return {
        "points": points,
        "path": line,
        # Closed back along the floor, so the line can carry a soft fill under
        # it without a second set of coordinates.
        "area": f"{coords[0][0]},{CHART_H - CHART_PAD} {line} "
                f"{coords[-1][0]},{CHART_H - CHART_PAD}",
        "count": len(points),
        "width": CHART_W,
        "height": CHART_H,
        "start": start,
        "low": low,
        "high": high,
    }


def _effect(row, leg_set: set[int], code: str, base: str) -> tuple[int, bool]:
    """What one entry did to this account's balance, in the account's currency.

    Both legs are counted, because a transfer between a card and the account
    behind it settles to the same place and nets to nothing — reading only one
    end would invent a movement.
    """
    delta = 0
    exact = True

    def amount(minor, currency):
        nonlocal exact
        if currency == code:
            return minor
        # Same call `balances.py` makes: convertible only when the account is in
        # base and the entry carries the rate it was captured with. Anything else
        # is guessing, and a guessed balance is worse than an admitted gap.
        if code == base and row["fx_rate_to_base"]:
            return convert_to_base(minor, row["fx_rate_to_base"])
        exact = False
        return 0

    if row["account_id"] in leg_set:
        if row["direction"] == "income":
            delta += amount(row["amount_minor"], row["currency"])
        else:
            delta -= amount(row["amount_minor"], row["currency"])

    if row["direction"] == "transfer" and row["counter_account_id"] in leg_set:
        delta += amount(row["counter_amount_minor"], row["counter_currency"])

    return delta, exact
