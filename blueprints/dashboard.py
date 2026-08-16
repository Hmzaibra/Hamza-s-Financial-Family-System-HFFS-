"""Dashboard.

Moved off `/` in Phase 1: the entry form is the product and now owns the root
route. The month figures and the recent list are the next piece of Phase 1 and
will both go through visibility_sql().
"""

from __future__ import annotations

from flask import Blueprint, g, render_template

from blueprints.auth import login_required
from db import base_currency, today_for

bp = Blueprint("dashboard", __name__)


@bp.get("/dashboard")
@login_required
def index():
    today = today_for(g.user["timezone"])
    return render_template(
        "dashboard.html",
        today=today,
        month_label=today.strftime("%B %Y"),
        currency=base_currency(),
    )
