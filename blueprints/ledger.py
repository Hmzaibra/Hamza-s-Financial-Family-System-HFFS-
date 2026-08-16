"""The transaction list, and editing or deleting a row from it.

Every read here composes `visibility_sql()` into the WHERE clause. The filters
are section 5's list — date range, person, category, account, merchant,
online/offline, free-text note search — and they are all server-side: the list
has to work with JavaScript off like everything else, and filtering in the
browser would only ever filter the page you happen to be looking at.
"""

from __future__ import annotations

from flask import (
    Blueprint, abort, flash, g, redirect, render_template, request, url_for,
)

import receipts
import transactions as txns
from blueprints.auth import login_required
from db import base_currency, query, query_one, today_for
from money import format_minor
from visibility import can_edit, visibility_sql

bp = Blueprint("ledger", __name__)

# Deliberately not a rolling window and not "this month": the most recent rows,
# whenever they happened. It answers "what did I just log" instantly and is
# never empty on the 1st. The header states the window so it cannot be mistaken
# for the month total on the dashboard, which counts something different.
DEFAULT_LIMIT = 50


def _filters() -> dict:
    """Read the querystring into a dict of applied filters."""
    def s(name):
        return (request.args.get(name) or "").strip()

    return {
        "from": s("from"), "to": s("to"), "user_id": s("user_id"),
        "category_id": s("category_id"), "account_id": s("account_id"),
        "merchant_id": s("merchant_id"), "online": s("online"),
        "direction": s("direction"), "q": s("q"),
    }


def _where(user, f: dict) -> tuple[str, list]:
    """Build the WHERE clause: the visibility rule first, then the filters."""
    vis_sql, params = visibility_sql(user)
    clauses = [vis_sql]

    if f["from"]:
        clauses.append("t.occurred_on >= ?"); params.append(f["from"])
    if f["to"]:
        clauses.append("t.occurred_on <= ?"); params.append(f["to"])
    if f["user_id"]:
        clauses.append("t.user_id = ?"); params.append(f["user_id"])
    if f["account_id"]:
        # An account filter means "money that moved through this", so a transfer
        # counts from either end — otherwise filtering by the destination of
        # every transfer you have ever made returns nothing.
        clauses.append("(t.account_id = ? OR t.counter_account_id = ?)")
        params.extend([f["account_id"], f["account_id"]])
    if f["merchant_id"]:
        clauses.append("t.merchant_id = ?"); params.append(f["merchant_id"])
    if f["direction"] in ("spend", "income", "transfer"):
        clauses.append("t.direction = ?"); params.append(f["direction"])
    if f["online"] in ("0", "1"):
        clauses.append("t.is_online = ?"); params.append(f["online"])
    if f["category_id"]:
        # Picking a parent means the whole family. Choosing "Eating Out" and
        # getting nothing back because every row is filed under Coffee would be
        # a filter that punishes you for having organised your categories.
        clauses.append(
            "(t.category_id = ? OR t.category_id IN "
            "  (SELECT id FROM categories WHERE parent_id = ?))")
        params.extend([f["category_id"], f["category_id"]])
    if f["q"]:
        # Note text and merchant name, which is what people actually remember.
        clauses.append(
            "(t.note LIKE ? ESCAPE '\\' OR t.merchant_id IN "
            "  (SELECT id FROM merchants WHERE name LIKE ? ESCAPE '\\'))")
        needle = "%" + f["q"].replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        params.extend([needle, needle])

    return " AND ".join(clauses), params


@bp.get("/transactions")
@login_required
def index():
    f = _filters()
    where, params = _where(g.user, f)
    active = sum(1 for k, v in f.items() if v)

    rows = query(
        f"SELECT t.*, m.name AS merchant_name, "
        f"       c.name AS category_name, p.name AS parent_category, "
        f"       COALESCE(p.icon, c.icon) AS category_icon, "
        f"       a.name AS account_name, a.type AS account_type, "
        f"       ca.name AS counter_account_name, u.display_name AS owner_name "
        f"  FROM transactions t "
        f"  LEFT JOIN merchants  m  ON m.id  = t.merchant_id "
        f"  LEFT JOIN categories c  ON c.id  = t.category_id "
        f"  LEFT JOIN categories p  ON p.id  = c.parent_id "
        f"  LEFT JOIN accounts   a  ON a.id  = t.account_id "
        f"  LEFT JOIN accounts   ca ON ca.id = t.counter_account_id "
        f"  LEFT JOIN users      u  ON u.id  = t.user_id "
        f" WHERE {where} "
        f" ORDER BY t.occurred_on DESC, t.id DESC LIMIT ?",
        [*params, DEFAULT_LIMIT + 1],
    )

    more = len(rows) > DEFAULT_LIMIT
    rows = rows[:DEFAULT_LIMIT]

    # Grouped by day so the list reads like a diary rather than a spreadsheet.
    days: list[tuple[str, list]] = []
    for row in rows:
        if not days or days[-1][0] != row["occurred_on"]:
            days.append((row["occurred_on"], []))
        days[-1][1].append(row)

    return render_template(
        "ledger/index.html",
        days=days, count=len(rows), more=more, limit=DEFAULT_LIMIT,
        # One query for the whole page rather than one per line. Fifty round
        # trips to draw fifty paperclips is how a list gets slow on an SD card.
        photos=receipts.counts_for(row["id"] for row in rows),
        filters=f, active_filters=active,
        base=base_currency(),
        today=today_for(g.user["timezone"]),
        people=query("SELECT id, display_name FROM users ORDER BY display_name"),
        accounts=txns.active_accounts(),
        categories=txns.active_categories(),
        merchants=query(
            "SELECT id, name FROM merchants WHERE is_active = 1 ORDER BY name"),
        can_edit=can_edit,
    )


def _edit_values(row) -> dict:
    """A stored row as the form sees it.

    The template must not care whether it is rendering a row from the database
    or a rejected submission being handed back, so both arrive as a plain
    mapping with the same keys — including `amount` as text, since that is what
    the field holds. Passing the raw row on the way in and a MultiDict on the
    way out is how a template ends up formatting a column that only exists on
    one of them.
    """
    values = dict(row)
    values["amount"] = format_minor(row["amount_minor"], row["currency"])
    values["counter_amount"] = (
        format_minor(row["counter_amount_minor"], row["counter_currency"])
        if row["counter_amount_minor"] else "")
    return values


def _edit_view(row, values, error=None, status: int = 200):
    return (
        render_template(
            "ledger/edit.html", txn=row, values=values, error=error,
            accounts=txns.active_accounts(),
            categories=txns.active_categories(),
            merchants=query(
                "SELECT id, name, kind FROM merchants WHERE is_active = 1 ORDER BY name"),
            base=base_currency(),
            attachments=receipts.for_transaction(row["id"]),
        ),
        status,
    )


def _load_for_edit(txn_id: int):
    row = query_one("SELECT * FROM transactions WHERE id = ?", (txn_id,))
    if row is None:
        abort(404)
    if not can_edit(g.user, row):
        # Seeing a shared transaction never conferred the right to change it.
        abort(403)
    return row


@bp.get("/transactions/<int:txn_id>/edit")
@login_required
def edit(txn_id: int):
    row = _load_for_edit(txn_id)
    return _edit_view(row, _edit_values(row))[0]


@bp.post("/transactions/<int:txn_id>/edit")
@login_required
def save(txn_id: int):
    row = _load_for_edit(txn_id)
    try:
        txns.update_transaction(txn_id, g.user, request.form)
    except txns.EntryError as exc:
        # Hand back exactly what was typed, in the same shape a stored row
        # arrives in, so nothing is lost and nothing has to be special-cased.
        return _edit_view(row, request.form.to_dict(),
                          {"message": str(exc), "field": exc.field}, 400)
    flash("Saved.", "ok")
    return redirect(url_for("ledger.index"))


@bp.post("/transactions/<int:txn_id>/delete")
@login_required
def delete(txn_id: int):
    _load_for_edit(txn_id)
    if txns.delete_transaction(txn_id, g.user):
        flash("Deleted.", "ok")
    else:
        flash("That entry could not be deleted.", "error")
    return redirect(url_for("ledger.index"))
