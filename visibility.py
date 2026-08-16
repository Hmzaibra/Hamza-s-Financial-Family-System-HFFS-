"""The visibility rule (spec section 4). One implementation, used everywhere.

    Admin  → every transaction.
    Member → transactions where user_id = me OR is_shared = 1.

Visibility follows the transaction's owner, never the account. A shared bank
account must not expose one person's spending to everyone else who uses it,
which is why nothing here looks at accounts.owner_id.

Why a SQL fragment and not a function returning rows: the monthly summary, the
per-category totals and the Phase 3 limit evaluation are all transaction queries
too, and they aggregate rather than list. A row-returning helper would quietly
get bypassed by exactly those callers — the ones where a leak is hardest to
notice, because a wrong total looks like a total.

    sql, params = visibility_sql(user)
    rows = query(
        f"SELECT ... FROM transactions t WHERE {sql} AND t.occurred_on >= ?",
        [*params, start],
    )

The f-string above interpolates only this module's own literal text, never user
input; the parameters stay bound. That is the single exception to hard rule 5
and it is why this function returns its params alongside the fragment.
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence

ADMIN = "admin"


def visibility_sql(user: Mapping[str, Any], alias: str = "t") -> tuple[str, list]:
    """Return a SQL boolean fragment plus its bound parameters.

    `alias` is the table alias used for `transactions` in the calling query.
    """
    if user is None:
        # No session, no transactions. Fails closed rather than open.
        return "0 = 1", []

    if user["role"] == ADMIN:
        return "1 = 1", []

    return f"({alias}.user_id = ? OR {alias}.is_shared = 1)", [user["id"]]


def can_view(user: Mapping[str, Any], txn: Mapping[str, Any]) -> bool:
    """Same rule, applied to a single row already in hand.

    For post-fetch assertions and template guards. Not a substitute for putting
    visibility_sql() in the query — filtering in Python after fetching means the
    row was read, which is the thing to avoid.
    """
    if user is None:
        return False
    if user["role"] == ADMIN:
        return True
    return txn["user_id"] == user["id"] or bool(txn["is_shared"])


def can_edit(user: Mapping[str, Any], txn: Mapping[str, Any]) -> bool:
    """Who may change a transaction.

    Seeing a shared transaction does not confer the right to edit it; that stays
    with its owner and with admin.
    """
    if user is None:
        return False
    return user["role"] == ADMIN or txn["user_id"] == user["id"]


def visible_transaction_ids(rows: Sequence[Mapping[str, Any]], user) -> list[int]:
    """Defensive filter for anything that assembled rows outside a single query."""
    return [row["id"] for row in rows if can_view(user, row)]
