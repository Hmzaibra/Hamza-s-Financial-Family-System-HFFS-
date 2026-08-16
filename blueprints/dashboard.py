"""Dashboard: what the month cost, and where it went.

Both figures are ordinary transaction reads, so both compose `visibility_sql()`.
The balances on the accounts screen are the exception to that rule, not these.
"""

from __future__ import annotations

from flask import Blueprint, g, render_template

import balances as bal
import limits as budgets
from blueprints.auth import login_required
from db import base_currency, today_for
from visibility import visibility_sql

bp = Blueprint("dashboard", __name__)


@bp.get("/dashboard")
@login_required
def index():
    today = today_for(g.user["timezone"])
    vis_sql, vis_params = visibility_sql(g.user)

    total, unconverted = bal.month_spend(g.user, today, vis_sql, vis_params)
    income, income_unconverted = bal.month_income(g.user, today, vis_sql, vis_params)
    counts = bal.month_counts(g.user, today, vis_sql, vis_params)
    by_category = bal.month_by_category(g.user, today, vis_sql, vis_params)

    # Shares of the month, for the bar under each row. Integer maths on the
    # widths too — a percentage is not money, but there is no reason to reach
    # for a float when the largest row is the denominator.
    biggest = max((c["minor"] for c in by_category), default=0)
    for c in by_category:
        c["share"] = (c["minor"] * 100 // biggest) if biggest else 0

    return render_template(
        "dashboard.html",
        today=today,
        month_label=today.strftime("%B %Y"),
        currency=base_currency(),
        total=total,
        unconverted=unconverted + income_unconverted,
        income=income,
        # Net is only a sentence worth printing when both sides exist. On a month
        # of pure spending it is the spend total with a minus sign, which is the
        # same number said twice.
        net=income - total,
        counts=counts,
        logged=sum(counts.values()),
        by_category=by_category,
        # Budgets read across everyone, which is the second stated exception to
        # rule 4 — limits.py's docstring is where that argument lives. What is
        # filtered is which budgets this person may see at all, not the figures
        # inside them.
        budgets=budgets.dashboard_limits(g.user, today),
    )
