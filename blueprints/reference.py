"""Accounts, categories and merchants — the reference data the entry form eats.

Admin-only (spec section 3). Nothing here hard-deletes: accounts and categories
carry transactions, and a merchant you stop using should disappear from the
chips without rewriting last March's history. `is_active = 0` everywhere.
"""

from __future__ import annotations

import re

from flask import Blueprint, abort, flash, g, redirect, render_template, request, url_for

from blueprints.auth import admin_required
from db import execute, query, query_one, today_for, utc_now
from money import MoneyError, parse_to_minor
from transactions import deactivate_expired_cards

bp = Blueprint("reference", __name__, url_prefix="/settings")

ACCOUNT_TYPES = ("bank", "credit_card", "debit_card", "instapay", "cash", "wallet")
CURRENCIES = ("EGP", "EUR", "USD", "GBP", "AED", "SAR")

# A card is plastic with a network on it; the other four are places money sits.
CARD_TYPES = ("credit_card", "debit_card")
# What can hold a link, and what can be one. A credit card is its own thing —
# the bank lends it money, it does not draw on your balance — and cash is linked
# to nothing at all.
PARENT_TYPES = ("bank", "wallet")
LINKABLE_TYPES = ("instapay", "debit_card")

CARD_NETWORKS = (
    "Visa", "Mastercard", "Meeza", "American Express", "UnionPay", "Discover", "Other",
)

# Any colour the household likes, as long as it is something a browser will
# actually paint: a hex value or a plain CSS colour name. It is rendered as an
# SVG fill attribute rather than an inline style, because the CSP forbids those,
# and this pattern is what keeps the attribute from becoming an injection point.
COLOR_RE = re.compile(r"^#(?:[0-9a-fA-F]{3,4}|[0-9a-fA-F]{6}|[0-9a-fA-F]{8})$|^[A-Za-z]{3,20}$")

# 'YYYY-MM'. A card is good through the end of its printed month, so the day is
# not something anyone has to give.
EXPIRY_RE = re.compile(r"^(20\d{2})-(0[1-9]|1[0-2])$")

# @something. Instapay handles are the part after the @, and people write them
# with and without it, so the @ is normalised on rather than demanded.
HANDLE_RE = re.compile(r"^@[A-Za-z0-9._-]{2,40}$")


def _positive_minor(raw: str, currency: str, label: str) -> tuple[int | None, str | None]:
    """Parse a required, strictly positive money field. Returns (value, error)."""
    raw = (raw or "").strip()
    if not raw:
        return None, f"{label} is required."
    if raw.startswith("-"):
        return None, f"{label} cannot be negative."
    try:
        return parse_to_minor(raw, currency), None
    except MoneyError as exc:
        return None, f"{label}: {exc}."


@bp.get("/")
@admin_required
def index():
    counts = {
        "accounts": query_one("SELECT COUNT(*) n FROM accounts WHERE is_active = 1")["n"],
        "categories": query_one("SELECT COUNT(*) n FROM categories WHERE is_active = 1")["n"],
        "merchants": query_one("SELECT COUNT(*) n FROM merchants WHERE is_active = 1")["n"],
    }
    return render_template("settings/index.html", counts=counts)


# ------------------------------------------------------------------ accounts


@bp.get("/accounts")
@admin_required
def accounts():
    # Cards expire on their own schedule, so the list checks before it draws
    # rather than trusting a flag someone last touched a year ago.
    #
    # The reader's own calendar day, not the server's (invariant 3). The entry
    # form already did this; using date.today() here meant a card could read as
    # live on one screen and expired on the other for up to a day, depending on
    # where the Pi sits relative to the person holding the card.
    expired = deactivate_expired_cards(today_for(g.user["timezone"]))
    if expired:
        flash(f"{', '.join(expired)} expired and "
              f"{'has' if len(expired) == 1 else 'have'} been switched off.", "error")

    rows = query(
        "SELECT a.*, u.display_name AS owner_name FROM accounts a "
        "LEFT JOIN users u ON u.id = a.owner_id "
        "ORDER BY a.is_active DESC, a.sort_order, a.name"
    )
    # Linked cards are listed underneath the account they draw on rather than as
    # peers of it, because that is how the money actually works: the card is a
    # way of reaching the bank balance, not a second balance.
    children: dict[int, list] = {}
    for row in rows:
        if row["parent_account_id"] is not None:
            children.setdefault(row["parent_account_id"], []).append(row)

    return render_template(
        "settings/accounts.html",
        accounts=[r for r in rows if r["parent_account_id"] is None],
        children=children,
        parent_types=PARENT_TYPES,
    )


def _account_form_view(account, values, error=None, status: int = 200):
    return (
        render_template(
            "settings/account_form.html",
            account=account,
            users=query(
                "SELECT id, display_name FROM users WHERE is_active = 1 ORDER BY display_name"),
            # Only a standalone bank or wallet can be on the receiving end of a
            # link, and nothing may be linked to itself.
            parents=query(
                "SELECT id, name, type, currency, owner_id FROM accounts "
                "WHERE is_active = 1 AND type IN ('bank','wallet') "
                "  AND parent_account_id IS NULL AND id IS NOT ? "
                "ORDER BY sort_order, name",
                (account["id"] if account else -1,),
            ),
            types=ACCOUNT_TYPES,
            currencies=CURRENCIES,
            networks=CARD_NETWORKS,
            card_types=list(CARD_TYPES),
            linkable_types=list(LINKABLE_TYPES),
            error=error,
            values=values,
        ),
        status,
    )


@bp.get("/accounts/new")
@bp.get("/accounts/<int:account_id>")
@admin_required
def account_form(account_id: int | None = None):
    account = None
    if account_id is not None:
        account = query_one("SELECT * FROM accounts WHERE id = ?", (account_id,))
        if account is None:
            abort(404)

    # The + buttons on the accounts list arrive here with the type and the
    # parent already decided, so hanging a card off a bank is a name and a save.
    values: dict[str, str] = {}
    if account is None:
        wanted = (request.args.get("type") or "").strip()
        if wanted in ACCOUNT_TYPES:
            values["type"] = wanted
        parent_id = request.args.get("parent", type=int)
        if parent_id:
            parent = query_one(
                "SELECT id, currency, owner_id FROM accounts "
                "WHERE id = ? AND type IN ('bank','wallet')",
                (parent_id,),
            )
            if parent:
                values["parent_account_id"] = str(parent["id"])
                values["currency"] = parent["currency"]
                # A card on someone's account is theirs until someone says
                # otherwise. A default, not a rule — a joint account with a card
                # each is a real arrangement, so the full list stays offered.
                values["owner_id"] = str(parent["owner_id"] or "")

    return _account_form_view(account, values)


def _save_error(exc: Exception, name: str) -> str:
    """Turn a constraint failure into a sentence.

    The triggers in migration 003 are the backstop, not the user interface, so
    everything they can raise is checked in Python above as well. This exists
    for the cases only the database can see — a race between two phones, or a
    rule added later that the form has not caught up with.
    """
    text = str(exc)
    if "ux_accounts_one_instapay" in text:
        return "That account already has an Instapay handle. Only one is allowed."
    if "UNIQUE" in text and "name" in text:
        return f"An account called {name!r} already exists."
    if text.startswith("accounts: "):
        return text[len("accounts: "):].strip().capitalize() + "."
    return "That account could not be saved."


@bp.post("/accounts/new")
@bp.post("/accounts/<int:account_id>")
@admin_required
def account_save(account_id: int | None = None):
    form = request.form
    account = None
    if account_id is not None:
        account = query_one("SELECT * FROM accounts WHERE id = ?", (account_id,))
        if account is None:
            abort(404)

    def fail(message: str):
        return _account_form_view(account, form, message, 400)

    name = (form.get("name") or "").strip()
    acc_type = (form.get("type") or "").strip()
    currency = (form.get("currency") or "EGP").strip().upper()
    owner_id = form.get("owner_id") or None
    sort_order = form.get("sort_order") or "100"
    is_active = 1 if form.get("is_active") else 0

    if not name:
        return fail("Give the account a name.")
    if acc_type not in ACCOUNT_TYPES:
        return fail("Pick an account type.")
    if len(currency) != 3 or not currency.isalpha():
        return fail("Pick a currency.")

    # ---------------------------------------------------- instapay handle
    # Two boxes, one stored name. The handle is what identifies the account to
    # anyone sending money; the name is who it belongs to. Neither is much use
    # in a list without the other, so the list shows "Sam - @sam_pay"
    # and the form keeps them apart for editing.
    handle = None
    if acc_type == "instapay":
        handle = (form.get("instapay_handle") or "").strip()
        if handle and not handle.startswith("@"):
            handle = "@" + handle
        if not handle or handle == "@":
            return fail("An Instapay account needs a handle, like @sam_pay.")
        if not HANDLE_RE.match(handle):
            return fail(
                "That handle has characters Instapay does not use — letters, digits, "
                "dots, dashes and underscores only."
            )
        name = f"{name} - {handle}"

    # ---------------------------------------------------------------- links
    parent_id = None
    if acc_type in LINKABLE_TYPES:
        label = "An Instapay handle" if acc_type == "instapay" else "A debit card"
        parent_id = form.get("parent_account_id") or None
        if not parent_id:
            return fail(f"{label} has to be linked to the bank account or wallet it draws on.")

        parent = query_one("SELECT * FROM accounts WHERE id = ?", (parent_id,))
        if parent is None:
            return fail("That linked account no longer exists.")
        if parent["type"] not in PARENT_TYPES:
            return fail("Only a bank account or a wallet can hold cards and Instapay.")
        if parent["parent_account_id"] is not None:
            return fail("Links are one level deep — link to the account itself, not to a card.")
        if account is not None and parent["id"] == account["id"]:
            return fail("An account cannot be linked to itself.")

        # It is the same money reached a different way, so it is the same
        # currency. Letting these differ would invite a balance that is the sum
        # of two units.
        currency = parent["currency"]

        if acc_type == "instapay":
            clash = query_one(
                "SELECT name FROM accounts WHERE type = 'instapay' AND parent_account_id = ? "
                "AND id IS NOT ?",
                (parent_id, account["id"] if account else -1),
            )
            if clash:
                return fail(
                    f"{parent['name']} already has an Instapay handle ({clash['name']}). "
                    "One per account — debit cards can be as many as you like."
                )
    elif account is not None and account["parent_account_id"] is not None:
        # Re-typed out of being a link; the pointer goes with it.
        parent_id = None

    # ------------------------------------------------------ opening balance
    opening = 0
    raw = (form.get("opening_balance") or "").strip()
    if raw:
        negative = raw.startswith("-")
        # A credit card is a debt instrument, so it is the one thing that can
        # legitimately start below zero. Everywhere else a minus sign is a typo.
        if negative and acc_type != "credit_card":
            return fail(
                "Only a credit card can open in debt. Everything else takes a positive "
                "opening balance."
            )
        try:
            opening = parse_to_minor(raw.lstrip("-+"), currency)
        except MoneyError as exc:
            return fail(f"Opening balance: {exc}.")
        opening = -opening if negative else opening

    if acc_type == "instapay" and opening:
        return fail(
            "Instapay spends the balance of the account behind it, so it does not carry "
            "an opening balance of its own."
        )

    # --------------------------------------------------------- card details
    network = color = expires_on = None
    withdrawal = local_limit = intl_limit = None
    if acc_type in CARD_TYPES:
        network = (form.get("card_network") or "").strip()
        if network not in CARD_NETWORKS:
            return fail("Pick the card's network.")

        color = (form.get("card_color") or "").strip()
        if not COLOR_RE.match(color):
            return fail(
                "Give the card a colour — a hex value like #1F6F63, or a plain colour "
                "name like teal."
            )

        # A `month` input posts 'YYYY-MM' natively; a browser without one falls
        # back to a text box, which is why the pattern is checked rather than
        # trusted.
        expires_on = (form.get("card_expires_on") or "").strip()
        if not expires_on:
            return fail("Give the card its expiry date.")
        if not EXPIRY_RE.match(expires_on):
            return fail("Expiry should look like 2029-07 — the year and month on the card.")

        # An expired card is not an option to spend from, so saving one switches
        # it off rather than arguing about it. Re-dating it brings it back.
        # Same calendar as the deactivation sweep above, for the same reason.
        if expires_on < today_for(g.user["timezone"]).strftime("%Y-%m"):
            is_active = 0

        withdrawal, err = _positive_minor(
            form.get("withdrawal_limit"), currency, "Daily cash withdrawal limit")
        if err:
            return fail(err)

        if acc_type == "credit_card":
            local_limit, err = _positive_minor(
                form.get("credit_limit_local"), currency, "Monthly local limit")
            if err:
                return fail(err)
            # In Egypt the international limit is usually the smaller of the two,
            # so it is asked for separately and the tick is the shortcut, not the
            # default.
            if form.get("same_limits"):
                intl_limit = local_limit
            else:
                intl_limit, err = _positive_minor(
                    form.get("credit_limit_intl"), currency, "Monthly international limit")
                if err:
                    return fail(err)

    fields = (
        name, acc_type, currency, owner_id, opening, is_active, int(sort_order or 100),
        parent_id, network, color, withdrawal, local_limit, intl_limit, expires_on, handle,
    )

    try:
        if account is None:
            execute(
                "INSERT INTO accounts (name, type, currency, owner_id, opening_balance_minor, "
                "is_active, sort_order, parent_account_id, card_network, card_color, "
                "withdrawal_limit_minor, credit_limit_local_minor, credit_limit_intl_minor, "
                "card_expires_on, instapay_handle, created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                fields + (utc_now(),),
            )
            flash(f"Added {name}.", "ok")
        else:
            execute(
                "UPDATE accounts SET name=?, type=?, currency=?, owner_id=?, "
                "opening_balance_minor=?, is_active=?, sort_order=?, parent_account_id=?, "
                "card_network=?, card_color=?, withdrawal_limit_minor=?, "
                "credit_limit_local_minor=?, credit_limit_intl_minor=?, card_expires_on=?, "
                "instapay_handle=? WHERE id=?",
                fields + (account_id,),
            )
            # Instapay is the same money as the account behind it, so a currency
            # change on the parent has to reach the handle too.
            if acc_type in PARENT_TYPES:
                execute(
                    "UPDATE accounts SET currency = ? WHERE parent_account_id = ?",
                    (currency, account_id),
                )
            flash(f"Saved {name}.", "ok")
    except Exception as exc:
        return fail(_save_error(exc, name))

    return redirect(url_for("reference.accounts"))


# ---------------------------------------------------------------- categories


@bp.get("/categories")
@admin_required
def categories():
    rows = query(
        "SELECT c.*, p.name AS parent_name, "
        "  (SELECT COUNT(*) FROM transactions t WHERE t.category_id = c.id) AS uses "
        "FROM categories c LEFT JOIN categories p ON p.id = c.parent_id "
        "ORDER BY COALESCE(p.sort_order, c.sort_order), COALESCE(p.name, c.name), "
        "         c.parent_id IS NOT NULL, c.sort_order, c.name"
    )
    return render_template("settings/categories.html", categories=rows)


@bp.get("/categories/new")
@bp.get("/categories/<int:category_id>")
@admin_required
def category_form(category_id: int | None = None):
    category = None
    if category_id is not None:
        category = query_one("SELECT * FROM categories WHERE id = ?", (category_id,))
        if category is None:
            abort(404)
    return render_template(
        "settings/category_form.html",
        category=category,
        # Only top-level categories may be parents — one level, no deeper.
        parents=query(
            "SELECT id, name FROM categories WHERE parent_id IS NULL AND is_active = 1 "
            "AND id IS NOT ? ORDER BY sort_order, name",
            (category_id or -1,),
        ),
        error=None, values={},
    )


@bp.post("/categories/new")
@bp.post("/categories/<int:category_id>")
@admin_required
def category_save(category_id: int | None = None):
    form = request.form
    name = (form.get("name") or "").strip()
    parent_id = form.get("parent_id") or None
    icon = (form.get("icon") or "").strip() or None
    sort_order = int(form.get("sort_order") or 100)
    is_active = 1 if form.get("is_active") else 0

    def fail(message: str):
        return (
            render_template(
                "settings/category_form.html",
                category=query_one("SELECT * FROM categories WHERE id = ?", (category_id,))
                if category_id else None,
                parents=query(
                    "SELECT id, name FROM categories WHERE parent_id IS NULL AND is_active = 1 "
                    "AND id IS NOT ? ORDER BY sort_order, name", (category_id or -1,)),
                error=message, values=form,
            ),
            400,
        )

    if not name:
        return fail("Give the category a name.")

    # The database enforces one level via trigger; catching it here turns an
    # IntegrityError into a sentence.
    if parent_id:
        parent = query_one("SELECT parent_id FROM categories WHERE id = ?", (parent_id,))
        if parent is None:
            return fail("That parent category no longer exists.")
        if parent["parent_id"] is not None:
            return fail("Categories only nest one level deep.")

    try:
        if category_id is None:
            execute(
                "INSERT INTO categories (name, parent_id, icon, is_active, sort_order) "
                "VALUES (?,?,?,?,?)",
                (name, parent_id, icon, is_active, sort_order),
            )
        else:
            execute(
                "UPDATE categories SET name=?, parent_id=?, icon=?, is_active=?, sort_order=? "
                "WHERE id=?",
                (name, parent_id, icon, is_active, sort_order, category_id),
            )
    except Exception:
        return fail(f"A category called {name!r} already exists there.")

    flash(f"Saved {name}.", "ok")
    return redirect(url_for("reference.categories"))


# ----------------------------------------------------------------- merchants


@bp.get("/merchants")
@admin_required
def merchants():
    rows = query(
        "SELECT m.*, c.name AS category_name, a.name AS account_name, "
        "  (SELECT COUNT(*) FROM transactions t WHERE t.merchant_id = m.id) AS uses "
        "FROM merchants m "
        "LEFT JOIN categories c ON c.id = m.default_category_id "
        "LEFT JOIN accounts   a ON a.id = m.default_account_id "
        "ORDER BY m.is_active DESC, m.name"
    )
    return render_template("settings/merchants.html", merchants=rows)


@bp.get("/merchants/<int:merchant_id>")
@admin_required
def merchant_form(merchant_id: int):
    merchant = query_one("SELECT * FROM merchants WHERE id = ?", (merchant_id,))
    if merchant is None:
        abort(404)
    return render_template(
        "settings/merchant_form.html",
        merchant=merchant,
        categories=query(
            "SELECT c.id, c.name, p.name AS parent_name FROM categories c "
            "LEFT JOIN categories p ON p.id = c.parent_id WHERE c.is_active = 1 "
            "ORDER BY COALESCE(p.name, c.name), c.parent_id IS NOT NULL, c.name"),
        accounts=query(
            "SELECT id, name FROM accounts WHERE is_active = 1 ORDER BY sort_order, name"),
        error=None,
    )


@bp.post("/merchants/<int:merchant_id>")
@admin_required
def merchant_save(merchant_id: int):
    merchant = query_one("SELECT * FROM merchants WHERE id = ?", (merchant_id,))
    if merchant is None:
        abort(404)

    form = request.form
    name = (form.get("name") or "").strip()
    if not name:
        flash("Give the merchant a name.", "error")
        return redirect(url_for("reference.merchant_form", merchant_id=merchant_id))

    is_active = 1 if form.get("is_active") else 0

    # Every merchant is either somewhere you spend or someone who pays you. The
    # 'both' value survives in the schema from before migration 004 retired the
    # Receipt-less system merchant, but nothing creates one any more.
    kind = (form.get("kind") or "").strip()
    if kind not in ("spend", "income"):
        kind = merchant["kind"] if merchant["kind"] in ("spend", "income") else "spend"

    try:
        execute(
            "UPDATE merchants SET name=?, default_category_id=?, default_account_id=?, "
            "default_is_online=?, notes=?, is_active=?, kind=? WHERE id=?",
            (
                name,
                form.get("default_category_id") or None,
                form.get("default_account_id") or None,
                1 if form.get("default_is_online") else 0,
                (form.get("notes") or "").strip() or None,
                is_active,
                kind,
                merchant_id,
            ),
        )
    except Exception:
        flash(f"A merchant called {name!r} already exists.", "error")
        return redirect(url_for("reference.merchant_form", merchant_id=merchant_id))

    flash(f"Saved {name}.", "ok")
    return redirect(url_for("reference.merchants"))
