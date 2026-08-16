"""My accounts: what I have, what each one did, and how its balance got there.

Three screens that answer three different questions, deliberately kept apart
rather than stacked on one page:

    /accounts               which accounts are mine, and what is in them
    /accounts/<id>          what this one did this month
    /accounts/<id>/history  what the balance was after every entry, ever

The third is the expensive one and is not on the path of the first two. Someone
glancing at a balance should not pay for a full statement walk.

This is *not* `/settings/accounts`. That screen is admin-only and is where
accounts are created and edited; this one is for everybody and is read-only.
Two screens showing the same rows is usually a smell, but these are the two
halves of a genuine split: one is administration and one is looking at your
money, and the second is the reason the app exists.
"""

from __future__ import annotations

from flask import Blueprint, abort, g, render_template, request

import accounts as acct
import balances as bal
from blueprints.auth import login_required
from db import base_currency, query_one, today_for
from visibility import visibility_sql

bp = Blueprint("myaccounts", __name__, url_prefix="/accounts")

# How much of the statement arrives on the first load. Twenty is about two
# thumb-scrolls on a phone, and the page offers more in the same size steps
# rather than an infinite scroll: a link that says how many is honest about the
# cost, and it works with JavaScript off.
PAGE = 20
MAX_PAGE = 2000


def _account_or_404(account_id: int):
    row = query_one("SELECT * FROM accounts WHERE id = ?", (account_id,))
    if row is None:
        abort(404)
    if not acct.may_see(g.user, row):
        # 404 rather than 403: which accounts exist in this household is not
        # something a member needs told about the ones that are not theirs.
        abort(404)
    return row


@bp.get("/")
@login_required
def index():
    rows = acct.visible_accounts(g.user)
    money = bal.balances_for_display()

    # Cards and Instapay handles sit under the account they draw on, because
    # that is how the money works — the card is a way of reaching the balance,
    # not a second one.
    children: dict[int, list] = {}
    for row in rows:
        if row["parent_account_id"] is not None:
            children.setdefault(row["parent_account_id"], []).append(row)

    top = [r for r in rows if r["parent_account_id"] is None]
    # A card whose parent this person cannot see would otherwise vanish, which
    # is worse than showing it on its own: it is still their card.
    orphans = [
        r for r in rows
        if r["parent_account_id"] is not None
        and r["parent_account_id"] not in {t["id"] for t in top}
    ]

    return render_template(
        "myaccounts/index.html",
        accounts=top + orphans,
        children=children,
        balances=money,
        owners=acct.owner_names(),
        overdrawn={r["id"]: bal.is_overdrawn(r, money.get(r["id"])) for r in rows},
        base=base_currency(),
    )


@bp.get("/<int:account_id>")
@login_required
def summary(account_id: int):
    account = _account_or_404(account_id)
    today = today_for(g.user["timezone"])
    vis_sql, vis_params = visibility_sql(g.user)

    settlement_id = acct.settles_to(account_id)
    money = bal.account_balances().get(settlement_id)

    return render_template(
        "myaccounts/summary.html",
        account=account,
        # A card reports the balance of the account behind it. That is not a
        # convenience — it is the answer to "how much can I spend with this",
        # which is the only question the number is there for.
        settlement=query_one("SELECT id, name, currency FROM accounts WHERE id = ?",
                             (settlement_id,)),
        balance=money,
        overdrawn=bal.is_overdrawn(account, money),
        month=acct.month_here(account_id, today, vis_sql, vis_params),
        wheels=acct.wheels(account, today),
        owners=acct.owners_of(account_id),
        month_label=today.strftime("%B %Y"),
        base=base_currency(),
    )


@bp.get("/<int:account_id>/history")
@login_required
def history(account_id: int):
    account = _account_or_404(account_id)
    vis_sql, vis_params = visibility_sql(g.user)

    # `?show=` grows in PAGE-sized steps and is the whole of the pagination
    # state, so "show me more" stays a link. Clamped because the number arrives
    # from a querystring and the walk below is O(everything).
    show = request.args.get("show", type=int) or PAGE
    show = max(PAGE, min(show, MAX_PAGE))

    return render_template(
        "myaccounts/history.html",
        account=account,
        show=show,
        step=PAGE,
        history=acct.history(account, g.user, vis_sql, vis_params, limit=show),
    )
