"""Account balances, and the month figures that read from the same rows.

Two things in here deliberately do not follow the usual rule.

**Balances bypass `visibility_sql()`.** That is the stated exception to rule 4,
and it is stated because the alternative is worse: a member looking at a shared
bank account would otherwise see a balance computed from a subset of its
transactions, which is not a partial answer but a *wrong* one — a number that
disagrees with the bank app and quietly teaches you to distrust the screen.
Aggregate totals leak; individual transactions never do. The month figures below
are ordinary transaction reads and *do* go through the filter.

**Money spent on a card comes out of the account behind it.** A debit card and
an Instapay handle are ways of reaching a bank or wallet, not second pots, so
every leg is resolved to its *settlement account* — the parent if there is one,
otherwise the account itself — before anything is added up. This is the whole
meaning of the link, and it is why a card's displayed balance is its parent's.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from db import base_currency, query
from money import convert_to_base


@dataclass
class Balance:
    """A settlement account's balance, in that account's own currency."""

    account_id: int
    currency: str
    minor: int = 0
    # Legs the app could not express in this account's currency. See
    # _foreign_legs() for when that happens and why it declines to guess.
    unconverted: int = 0
    unconverted_currencies: set = field(default_factory=set)

    @property
    def approximate(self) -> bool:
        return self.unconverted > 0


def settlement_of() -> dict[int, int]:
    """account id → the account whose balance it actually moves.

    One level deep, which the `003` triggers enforce, so this needs no loop.
    """
    return {
        row["id"]: row["settles_to"]
        for row in query(
            "SELECT id, COALESCE(parent_account_id, id) AS settles_to FROM accounts"
        )
    }


# The three ways a transaction touches an account, as exact integer sums in the
# account's own currency. Anything in another currency is excluded here and
# picked up by _foreign_legs() — SQL does integer money only (rule 1).
_LEGS = (
    # spend and transfer-out reduce the account the money left from
    ("""SELECT sa.id AS acct, -SUM(t.amount_minor) AS delta
          FROM transactions t
          JOIN accounts a  ON a.id = t.account_id
          JOIN accounts sa ON sa.id = COALESCE(a.parent_account_id, a.id)
         WHERE t.direction IN ('spend','transfer') AND t.currency = sa.currency
         GROUP BY sa.id"""),
    # income increases it
    ("""SELECT sa.id AS acct, SUM(t.amount_minor) AS delta
          FROM transactions t
          JOIN accounts a  ON a.id = t.account_id
          JOIN accounts sa ON sa.id = COALESCE(a.parent_account_id, a.id)
         WHERE t.direction = 'income' AND t.currency = sa.currency
         GROUP BY sa.id"""),
    # a transfer's far side, which is why counter_amount_minor is always
    # populated: this sum never has to branch on NULL
    ("""SELECT sa.id AS acct, SUM(t.counter_amount_minor) AS delta
          FROM transactions t
          JOIN accounts a  ON a.id = t.counter_account_id
          JOIN accounts sa ON sa.id = COALESCE(a.parent_account_id, a.id)
         WHERE t.direction = 'transfer' AND t.counter_currency = sa.currency
         GROUP BY sa.id"""),
)


def _foreign_legs() -> list:
    """Legs recorded in a currency other than the settlement account's.

    A foreign charge on an Egyptian card is the common case, and it converts
    cleanly: the transaction carries the rate to base, and the account is in
    base. The other direction — a base-currency charge on a foreign-currency
    account — has no rate stored that points the right way, and inventing one
    would be arithmetic on two different things. Those legs are counted and
    surfaced rather than guessed at, the same call the international card limit
    makes.
    """
    return query(
        """SELECT sa.id AS acct, sa.currency AS account_currency,
                  t.amount_minor, t.currency, t.fx_rate_to_base, t.direction
             FROM transactions t
             JOIN accounts a  ON a.id = t.account_id
             JOIN accounts sa ON sa.id = COALESCE(a.parent_account_id, a.id)
            WHERE t.currency <> sa.currency
           UNION ALL
           SELECT sa.id, sa.currency,
                  t.counter_amount_minor, t.counter_currency, t.fx_rate_to_base, 'income'
             FROM transactions t
             JOIN accounts a  ON a.id = t.counter_account_id
             JOIN accounts sa ON sa.id = COALESCE(a.parent_account_id, a.id)
            WHERE t.direction = 'transfer' AND t.counter_currency <> sa.currency"""
    )


def account_balances() -> dict[int, Balance]:
    """Balance per settlement account, keyed by that account's id."""
    base = base_currency()

    out: dict[int, Balance] = {}
    for row in query(
        "SELECT COALESCE(parent_account_id, id) AS acct, "
        "       SUM(opening_balance_minor) AS opening "
        "FROM accounts GROUP BY COALESCE(parent_account_id, id)"
    ):
        out[row["acct"]] = Balance(account_id=row["acct"], currency="", minor=row["opening"] or 0)

    for row in query("SELECT id, currency FROM accounts WHERE parent_account_id IS NULL"):
        if row["id"] in out:
            out[row["id"]].currency = row["currency"]

    for sql in _LEGS:
        for row in query(sql):
            bal = out.get(row["acct"])
            if bal is not None and row["delta"] is not None:
                bal.minor += row["delta"]

    for leg in _foreign_legs():
        bal = out.get(leg["acct"])
        if bal is None:
            continue
        # Convertible only when the account is denominated in the base currency
        # and the leg carries the rate it was captured with.
        if bal.currency == base and leg["fx_rate_to_base"]:
            amount = convert_to_base(leg["amount_minor"], leg["fx_rate_to_base"])
            bal.minor += -amount if leg["direction"] in ("spend", "transfer") else amount
        else:
            bal.unconverted += 1
            bal.unconverted_currencies.add(leg["currency"])

    return out


def balances_for_display() -> dict[int, Balance]:
    """Balance for *every* account, linked ones included.

    A card and an Instapay handle report the balance of the account they draw
    on. That is not a convenience: it is the answer to "how much can I spend
    with this", which is the only question the row is there to answer.
    """
    settled = account_balances()
    return {
        acct: settled[settles_to]
        for acct, settles_to in settlement_of().items()
        if settles_to in settled
    }


def is_overdrawn(account, balance: Balance | None) -> bool:
    """Below zero on something that is not allowed to be.

    Only a credit card may sit in debt (rule 2). Everything else going negative
    is worth colouring, but never worth blocking a save over — see
    `transactions.overdraft_warning()`.
    """
    if balance is None or balance.approximate:
        return False
    return balance.minor < 0 and account["type"] != "credit_card"


# ------------------------------------------------------- the month figures
#
# Ordinary transaction reads, so these go through visibility_sql() like
# everything else. Only the balances above are exempt.


def month_bounds(day) -> tuple[str, str]:
    """[first of the month, first of the next) — half-open, so nothing is
    counted twice. Public because `accounts.py` asks the same question about one
    account, and a leading underscore two modules reach past is a lie about
    where the boundary is."""
    first = day.replace(day=1)
    if first.month == 12:
        nxt = first.replace(year=first.year + 1, month=1)
    else:
        nxt = first.replace(month=first.month + 1)
    return first.isoformat(), nxt.isoformat()


def month_spend(user, day, vis_sql: str, vis_params: list) -> tuple[int, int]:
    """(total spent in base currency, rows that could not be converted).

    The conversion happens here, in Python, through money.convert_to_base().
    `SUM(amount_minor * fx_rate_to_base)` in SQL would be float arithmetic on
    money, which rule 1 exists to forbid — and it would silently drop the NULL
    rate on every base-currency row besides.
    """
    start, end = month_bounds(day)
    base = base_currency()

    rows = query(
        f"SELECT t.amount_minor, t.currency, t.fx_rate_to_base FROM transactions t "
        f"WHERE {vis_sql} AND t.direction = 'spend' "
        f"  AND t.occurred_on >= ? AND t.occurred_on < ?",
        [*vis_params, start, end],
    )

    total = 0
    unconverted = 0
    for row in rows:
        if row["currency"] == base:
            total += row["amount_minor"]
        elif row["fx_rate_to_base"]:
            total += convert_to_base(row["amount_minor"], row["fx_rate_to_base"])
        else:
            unconverted += 1
    return total, unconverted


def month_income(user, day, vis_sql: str, vis_params: list) -> tuple[int, int]:
    """(income this month in base currency, rows that could not be converted).

    The month screen used to count spending and nothing else, which is right up
    until a household that has logged three entries — two of them income — opens
    it and is told nothing happened this month. "No spending" and "no activity"
    are different sentences and only one of them was true.
    """
    start, end = month_bounds(day)
    base = base_currency()

    rows = query(
        f"SELECT t.amount_minor, t.currency, t.fx_rate_to_base FROM transactions t "
        f"WHERE {vis_sql} AND t.direction = 'income' "
        f"  AND t.occurred_on >= ? AND t.occurred_on < ?",
        [*vis_params, start, end],
    )

    total = 0
    unconverted = 0
    for row in rows:
        if row["currency"] == base:
            total += row["amount_minor"]
        elif row["fx_rate_to_base"]:
            total += convert_to_base(row["amount_minor"], row["fx_rate_to_base"])
        else:
            unconverted += 1
    return total, unconverted


def month_counts(user, day, vis_sql: str, vis_params: list) -> dict:
    """How many entries of each kind landed this month.

    Purely so the screen can tell "you have logged nothing" apart from "you have
    logged things, none of which were spending". Transfers are counted and never
    totalled: moving your own money between your own accounts is not income and
    not expenditure, and adding it to either would double it.
    """
    start, end = month_bounds(day)
    return {
        row["direction"]: row["n"]
        for row in query(
            f"SELECT t.direction, COUNT(*) AS n FROM transactions t "
            f"WHERE {vis_sql} AND t.occurred_on >= ? AND t.occurred_on < ? "
            f"GROUP BY t.direction",
            [*vis_params, start, end],
        )
    }


def month_by_category(user, day, vis_sql: str, vis_params: list) -> list[dict]:
    """Spending this month grouped by top-level category, largest first.

    Grouped by the *parent* where there is one: "Eating Out" is the answer to
    where the money went, and splitting it across Coffee, Restaurant and
    Delivery on a phone-sized screen buries that.
    """
    start, end = month_bounds(day)
    base = base_currency()

    rows = query(
        f"SELECT COALESCE(p.id, c.id) AS group_id, "
        f"       COALESCE(p.name, c.name, 'Uncategorised') AS group_name, "
        f"       COALESCE(p.icon, c.icon) AS icon, "
        f"       t.amount_minor, t.currency, t.fx_rate_to_base "
        f"  FROM transactions t "
        f"  LEFT JOIN categories c ON c.id = t.category_id "
        f"  LEFT JOIN categories p ON p.id = c.parent_id "
        f" WHERE {vis_sql} AND t.direction = 'spend' "
        f"   AND t.occurred_on >= ? AND t.occurred_on < ?",
        [*vis_params, start, end],
    )

    totals: dict = {}
    for row in rows:
        key = row["group_id"]
        bucket = totals.setdefault(
            key, {"name": row["group_name"], "icon": row["icon"], "minor": 0, "count": 0}
        )
        if row["currency"] == base:
            bucket["minor"] += row["amount_minor"]
        elif row["fx_rate_to_base"]:
            bucket["minor"] += convert_to_base(row["amount_minor"], row["fx_rate_to_base"])
        else:
            continue
        bucket["count"] += 1

    return sorted(
        (b for b in totals.values() if b["count"]),
        key=lambda b: b["minor"],
        reverse=True,
    )
