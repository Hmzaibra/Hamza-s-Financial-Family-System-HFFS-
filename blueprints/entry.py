"""The entry form. This screen is the product (spec section 6).

The bar it is built against: a Receipt-less purchase is type amount, tap chip,
tap Save. Everything else on the screen is pre-filled, collapsed, or absent.

It works with JavaScript switched off. The chips are radio inputs, the merchant
defaults are applied server-side for any field left blank, and the confirmation
toast is server-rendered. JavaScript only makes it faster: live filtering,
inline merchant add without a round trip, and auto-fill the moment a chip is
tapped rather than at save time.
"""

from __future__ import annotations

from flask import (
    Blueprint, flash, g, jsonify, redirect, render_template, request, url_for,
)

import fx
import receipts
import transactions as txns
from blueprints.auth import login_required
from db import base_currency, today_for
from money import format_minor, symbol

bp = Blueprint("entry", __name__)


def _merchant_side(kind: str) -> dict:
    """One side of the form's merchant list: recent ones, then everything else.

    Spending and income keep separate lists. They share a table, but who you buy
    from and who pays you are different questions, and mixing them puts an
    employer in the chip row while you are standing at a till.
    """
    merchants = txns.active_merchants(kind)
    recents = txns.recent_merchants(g.user["id"], kind)
    recent_ids = {row["id"] for row in recents}
    return {
        "recents": recents,
        "others": [m for m in merchants if m["id"] not in recent_ids],
    }


def _rate_hints(base: str) -> dict:
    """Cached rates, formatted for a text box rather than for arithmetic.

    These only pre-fill the field. What is stored against the transaction is
    whatever is in the box at save time, because the rate on the day of purchase
    is the only correct one and it cannot be recovered later.
    """
    hints = {}
    for code, row in fx.cached(base).items():
        rate = f"{row['rate']:.4f}".rstrip("0").rstrip(".")
        hints[code] = {"rate": rate, "age_days": row["age_days"]}
    return hints


def _form_context():
    today = today_for(g.user["timezone"])

    # A card expires whether or not anyone visits the settings screen, so the
    # check lives where accounts are read.
    expired = txns.deactivate_expired_cards(today)
    if expired:
        flash(f"{', '.join(expired)} expired and {'has' if len(expired) == 1 else 'have'} "
              f"been switched off. Update the expiry date in Setup → Accounts.", "error")

    base = base_currency()
    spend = _merchant_side("spend")
    income = _merchant_side("income")

    return {
        "accounts": txns.active_accounts(),
        "categories": txns.active_categories(),
        # The chip row is the till path, and the till path is spending. Income is
        # logged rarely and deliberately, so its sources sit under the search box
        # with everything else rather than competing for the fast row.
        "pinned": spend["recents"],
        "spend_listed": spend["others"],
        "income_listed": list(income["recents"]) + list(income["others"]),
        "today": today.isoformat(),
        "base": base,
        "base_symbol": symbol(base),
        "fx_rates": _rate_hints(base),
        "default_shared": int(g.user["default_shared"]),
    }


@bp.get("/")
@login_required
def form():
    ctx = _form_context()

    # Server-rendered confirmation, so Undo survives a page refresh and needs no
    # JavaScript. ?saved=<id> is the only state carried across the redirect.
    saved = request.args.get("saved", type=int)
    toast = None
    if saved:
        row = txns.undoable(saved, g.user)
        if row is not None:
            detail = txns.describe(saved)
            toast = {
                "id": saved,
                "amount": format_minor(detail["amount_minor"], detail["currency"]),
                "currency": detail["currency"],
                "what": detail.get("what") or "",
            }

    if not ctx["accounts"]:
        flash("Add an account first — the form needs somewhere to spend from.", "error")

    return render_template("entry.html", toast=toast, values={}, error=None, **ctx)


@bp.post("/")
@login_required
def create():
    # A photo taken at the till posts with the entry it belongs to. Attaching
    # afterwards would mean saving, waiting for a redirect and finding the row
    # again — a round trip in the middle of the one screen built to have none.
    photo = request.files.get("receipt")
    has_photo = photo is not None and bool(photo.filename)

    form = request.form
    if has_photo and form.get("receiptless"):
        # Both were ticked. The photo wins and _prepare() must not see the flag,
        # or it would refuse an entry that is about to be perfectly consistent.
        form = form.copy()
        form.pop("receiptless", None)

    try:
        txn_id = txns.create_transaction(g.user, form)
    except txns.EntryError as exc:
        # Re-render rather than redirect, so nothing typed is lost.
        ctx = _form_context()
        return (
            render_template(
                "entry.html",
                toast=None,
                values=request.form,
                error={"message": str(exc), "field": exc.field},
                **ctx,
            ),
            400,
        )

    if has_photo:
        # The entry is already saved. A photo that will not decode is worth a
        # sentence, never worth throwing away the purchase someone just logged.
        try:
            receipts.store(txn_id, photo)
        except receipts.ReceiptError as exc:
            flash(f"Saved, but the photo did not attach — {exc}", "error")

    return redirect(url_for("entry.form", saved=txn_id))


@bp.post("/entry/<int:txn_id>/undo")
@login_required
def undo(txn_id: int):
    if txns.undo(txn_id, g.user):
        flash("Removed.", "ok")
    else:
        flash("That entry can no longer be undone.", "error")
    return redirect(url_for("entry.form"))


@bp.post("/entry/merchants")
@login_required
def add_merchant_inline():
    """Inline 'Add <name>' — creates and returns the merchant without a reload.

    The no-JavaScript path does not use this: it posts new_merchant_name with
    the form and create_transaction() does the same thing server-side.
    """
    name = (request.form.get("name") or "").strip()
    # Which list it joins follows the side of the form it was typed on.
    kind = "income" if (request.form.get("direction") or "") == "income" else "spend"
    try:
        merchant_id = txns.find_or_create_merchant(name, kind)
    except txns.EntryError as exc:
        return jsonify({"error": str(exc)}), 400

    return jsonify({
        "id": merchant_id,
        "name": name,
        "kind": kind,
        "default_category_id": None,
        "default_account_id": None,
        "default_is_online": 0,
    })
