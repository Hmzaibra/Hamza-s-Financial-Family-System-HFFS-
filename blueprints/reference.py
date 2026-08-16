"""Accounts, categories, merchants, people and budgets — everything Setup holds.

Admin-only (spec section 3). Nothing here hard-deletes: accounts and categories
carry transactions, a merchant you stop using should disappear from the chips
without rewriting last March's history, and a person who leaves still owns the
entries they made. `is_active = 0` everywhere.
"""

from __future__ import annotations

import re
from zoneinfo import available_timezones

from flask import (
    Blueprint, abort, current_app, flash, g, redirect, render_template, request, url_for,
)
from werkzeug.security import generate_password_hash

import accounts as acct
import balances as bal
import limits as budgets
from blueprints.auth import admin_required
from db import base_currency, execute, query, query_one, today_for, utc_now
from money import MoneyError, format_minor, parse_to_minor
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

# What someone types to sign in. Deliberately narrow: this string ends up in
# login_attempts, in a URL when a redirect carries it, and in a password
# manager, and none of those want a space in it.
USERNAME_RE = re.compile(r"^[A-Za-z0-9._-]{2,32}$")

# Telegram chat ids are signed integers. A group chat's is negative, which is
# why the minus sign is allowed rather than treated as a typo.
TELEGRAM_CHAT_RE = re.compile(r"^-?\d{1,20}$")

# The timezone picker. `available_timezones()` is six hundred entries and a
# <select> that long on a phone is unusable; these are the ones this household
# plausibly lives in, and the validator still accepts any IANA name for the day
# that stops being true.
COMMON_TIMEZONES = (
    "Africa/Cairo", "Europe/Berlin", "Europe/London", "Europe/Amsterdam",
    "Europe/Paris", "Asia/Dubai", "Asia/Riyadh", "America/New_York", "UTC",
)


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
        "people": query_one("SELECT COUNT(*) n FROM users WHERE is_active = 1")["n"],
        "limits": query_one("SELECT COUNT(*) n FROM limits WHERE is_active = 1")["n"],
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
        "SELECT a.* FROM accounts a ORDER BY a.is_active DESC, a.sort_order, a.name")
    # Since 006 an account can carry several names, so the owner is a list and
    # arrives in one query rather than one join that can only ever return one.
    names = acct.owner_names()
    # Linked cards are listed underneath the account they draw on rather than as
    # peers of it, because that is how the money actually works: the card is a
    # way of reaching the bank balance, not a second balance.
    children: dict[int, list] = {}
    for row in rows:
        if row["parent_account_id"] is not None:
            children.setdefault(row["parent_account_id"], []).append(row)

    # Balances deliberately skip visibility_sql() — see balances.py for why a
    # filtered balance is a wrong number rather than a partial one.
    money = bal.balances_for_display()
    overdrawn = {r["id"]: bal.is_overdrawn(r, money.get(r["id"])) for r in rows}

    return render_template(
        "settings/accounts.html",
        accounts=[r for r in rows if r["parent_account_id"] is None],
        children=children,
        parent_types=PARENT_TYPES,
        balances=money,
        owners=names,
        overdrawn=overdrawn,
        any_overdrawn=any(overdrawn.values()),
    )


def _account_form_view(account, values, error=None, status: int = 200):
    return (
        render_template(
            "settings/account_form.html",
            account=account,
            users=query(
                "SELECT id, display_name FROM users WHERE is_active = 1 ORDER BY display_name"),
            # A set, so the template asks "is this person on it" rather than
            # "is this person the one".
            owner_ids=(values.get("owner_ids")
                       if isinstance(values.get("owner_ids"), (set, list))
                       else {int(v) for v in (values.getlist("owner_ids")
                                              if hasattr(values, "getlist") else [])
                             if str(v).isdigit()}),
            # Only a standalone bank or wallet can be on the receiving end of a
            # link, and nothing may be linked to itself.
            # A card hung off an account defaults to that account's people, so
            # the form has to know who they are before anything is picked.
            parent_owners=acct.owner_id_map(),
            parents=query(
                "SELECT id, name, type, currency FROM accounts "
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
    values: dict = {}
    if account is not None:
        values["owner_ids"] = acct.owner_ids(account_id)
    if account is None:
        wanted = (request.args.get("type") or "").strip()
        if wanted in ACCOUNT_TYPES:
            values["type"] = wanted
        parent_id = request.args.get("parent", type=int)
        if parent_id:
            parent = query_one(
                "SELECT id, currency FROM accounts "
                "WHERE id = ? AND type IN ('bank','wallet')",
                (parent_id,),
            )
            if parent:
                values["parent_account_id"] = str(parent["id"])
                values["currency"] = parent["currency"]
                # A card on someone's account is theirs until someone says
                # otherwise. A default, not a rule — a joint account with a card
                # each is a real arrangement, so the full list stays offered.
                values["owner_ids"] = acct.owner_ids(parent["id"])

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
    sort_order = form.get("sort_order") or "100"
    is_active = 1 if form.get("is_active") else 0

    # Several people may be named on one account (006). Nobody named means the
    # household's, which is a state rather than a gap — a joint account that
    # everybody uses does not need a list of everybody.
    real = {row["id"] for row in query("SELECT id FROM users WHERE is_active = 1")}
    owner_ids = [int(v) for v in form.getlist("owner_ids") if v.isdigit() and int(v) in real]

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
        name, acc_type, currency, opening, is_active, int(sort_order or 100),
        parent_id, network, color, withdrawal, local_limit, intl_limit, expires_on, handle,
    )

    try:
        if account is None:
            cur = execute(
                "INSERT INTO accounts (name, type, currency, opening_balance_minor, "
                "is_active, sort_order, parent_account_id, card_network, card_color, "
                "withdrawal_limit_minor, credit_limit_local_minor, credit_limit_intl_minor, "
                "card_expires_on, instapay_handle, created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                fields + (utc_now(),),
            )
            acct.set_owners(cur.lastrowid, owner_ids)
            flash(f"Added {name}.", "ok")
        else:
            execute(
                "UPDATE accounts SET name=?, type=?, currency=?, "
                "opening_balance_minor=?, is_active=?, sort_order=?, parent_account_id=?, "
                "card_network=?, card_color=?, withdrawal_limit_minor=?, "
                "credit_limit_local_minor=?, credit_limit_intl_minor=?, card_expires_on=?, "
                "instapay_handle=? WHERE id=?",
                fields + (account_id,),
            )
            acct.set_owners(account_id, owner_ids)
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


# -------------------------------------------------------------------- people
#
# Phase 3 needs this screen for one concrete reason: a Telegram alert goes to a
# chat id, and until now the only way to put one on a user was sqlite3 over SSH.
# It earns its place twice over, because `flask create-admin` was also the only
# way this household could gain a second member.


def _active_admins(excluding: int | None = None) -> int:
    return query_one(
        "SELECT COUNT(*) AS n FROM users WHERE role = 'admin' AND is_active = 1 "
        "AND id IS NOT ?",
        (excluding or -1,),
    )["n"]


@bp.get("/people")
@admin_required
def people():
    rows = query(
        "SELECT u.*, "
        "  (SELECT COUNT(*) FROM transactions t WHERE t.user_id = u.id) AS entries "
        "FROM users u ORDER BY u.is_active DESC, u.display_name"
    )
    return render_template(
        "settings/people.html",
        people=rows,
        telegram_ready=bool(current_app.config["TELEGRAM_BOT_TOKEN"]),
    )


def _person_form_view(person, values, error=None, status: int = 200):
    return (
        render_template(
            "settings/person_form.html",
            person=person, values=values, error=error,
            timezones=COMMON_TIMEZONES,
            telegram_ready=bool(current_app.config["TELEGRAM_BOT_TOKEN"]),
        ),
        status,
    )


@bp.get("/people/new")
@bp.get("/people/<int:user_id>")
@admin_required
def person_form(user_id: int | None = None):
    person = None
    if user_id is not None:
        person = query_one("SELECT * FROM users WHERE id = ?", (user_id,))
        if person is None:
            abort(404)
    values = dict(person) if person else {"timezone": current_app.config["DEFAULT_TIMEZONE"]}
    return _person_form_view(person, values)


@bp.post("/people/new")
@bp.post("/people/<int:user_id>")
@admin_required
def person_save(user_id: int | None = None):
    form = request.form
    person = None
    if user_id is not None:
        person = query_one("SELECT * FROM users WHERE id = ?", (user_id,))
        if person is None:
            abort(404)

    def fail(message: str):
        return _person_form_view(person, form, message, 400)

    display_name = (form.get("display_name") or "").strip()
    if not display_name:
        return fail("Give them a name to show on entries.")

    role = (form.get("role") or "member").strip()
    if role not in ("admin", "member"):
        return fail("Pick admin or member.")

    tz = (form.get("timezone") or "").strip()
    if tz not in available_timezones():
        return fail("Pick a timezone — it decides which calendar day their entries land on.")

    default_shared = 1 if form.get("default_shared") else 0
    is_active = 1 if form.get("is_active") else 0

    # A chat id is a signed integer from Telegram; a group chat's is negative.
    # Stored as text because it is an identifier, never a number to do sums with.
    chat_id = (form.get("telegram_chat_id") or "").strip() or None
    if chat_id and not TELEGRAM_CHAT_RE.match(chat_id):
        return fail("A Telegram chat id is a number, like 123456789. "
                    "Run `flask --app app telegram-chats` to find it.")

    password = form.get("password") or ""
    if password and len(password) < 8:
        return fail("Use at least 8 characters for a password.")

    # The last way back in. Demoting or switching off the only active admin
    # locks everybody out of the settings screens permanently, and the only cure
    # is sqlite3 on the Pi — so it is refused here rather than regretted later.
    if person is not None and (role != "admin" or not is_active):
        if person["role"] == "admin" and person["is_active"] and not _active_admins(person["id"]):
            return fail(
                f"{person['display_name']} is the only admin left. Make someone else an "
                f"admin first, or this account is the last way into Setup.")

    if person is None:
        username = (form.get("username") or "").strip()
        if not username:
            return fail("Pick a username — it is what they type to sign in.")
        if not USERNAME_RE.match(username):
            return fail("Usernames are letters, digits, dots, dashes and underscores.")
        if not password:
            return fail("Set a password. There is no reset link — you hand it to them.")
        try:
            execute(
                "INSERT INTO users (username, display_name, password_hash, role, "
                "default_shared, timezone, telegram_chat_id, is_active, created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (username, display_name, generate_password_hash(password), role,
                 default_shared, tz, chat_id, is_active, utc_now()),
            )
        except Exception:
            return fail(f"Someone already signs in as {username!r}.")
        flash(f"Added {display_name}.", "ok")
        return redirect(url_for("reference.people"))

    # The username is not editable. It is what login_attempts records against and
    # what someone has already typed into a phone's password manager; renaming it
    # buys nothing and breaks both.
    execute(
        "UPDATE users SET display_name=?, role=?, default_shared=?, timezone=?, "
        "telegram_chat_id=?, is_active=? WHERE id=?",
        (display_name, role, default_shared, tz, chat_id, is_active, user_id),
    )
    if password:
        execute("UPDATE users SET password_hash = ? WHERE id = ?",
                (generate_password_hash(password), user_id))

    flash(f"Saved {display_name}." + (" New password set." if password else ""), "ok")
    return redirect(url_for("reference.people"))


# ------------------------------------------------------------------- budgets
#
# Budgets warn, they never block. The `credit_limit_*` columns on `accounts` are
# a different idea wearing the same English word: those are ceilings a bank set,
# and transactions.py does refuse a save that would cross one.


@bp.get("/limits")
@admin_required
def limits():
    today = today_for(g.user["timezone"])
    rows = query("SELECT * FROM limits ORDER BY is_active DESC, scope_type, name")
    return render_template(
        "settings/limits.html",
        limits=[budgets.evaluate(row, today) for row in rows if row["is_active"]],
        archived=[row for row in rows if not row["is_active"]],
        scope_name=budgets.scope_name,
        currency=base_currency(),
    )


def _limit_form_view(limit, values, error=None, status: int = 200):
    return (
        render_template(
            "settings/limit_form.html",
            limit=limit, values=values, error=error,
            currency=base_currency(),
            scopes=budgets.SCOPES,
            periods=budgets.PERIODS,
            people=query(
                "SELECT id, display_name FROM users WHERE is_active = 1 ORDER BY display_name"),
            categories=query(
                "SELECT c.id, c.name, p.name AS parent_name FROM categories c "
                "LEFT JOIN categories p ON p.id = c.parent_id WHERE c.is_active = 1 "
                "ORDER BY COALESCE(p.name, c.name), c.parent_id IS NOT NULL, c.name"),
            accounts=query(
                "SELECT id, name FROM accounts WHERE is_active = 1 ORDER BY sort_order, name"),
            merchants=query(
                "SELECT id, name FROM merchants WHERE is_active = 1 ORDER BY name"),
        ),
        status,
    )


@bp.get("/limits/new")
@bp.get("/limits/<int:limit_id>")
@admin_required
def limit_form(limit_id: int | None = None):
    limit = None
    if limit_id is not None:
        limit = query_one("SELECT * FROM limits WHERE id = ?", (limit_id,))
        if limit is None:
            abort(404)
    values = {"period": "monthly", "warn_pct": 80, "scope": "household:", "is_active": 1}
    if limit:
        values = dict(limit)
        values["amount"] = format_minor(limit["amount_minor"], limit["currency"])
        values["scope"] = f"{limit['scope_type']}:{limit['scope_id'] or ''}"
    return _limit_form_view(limit, values)


@bp.post("/limits/new")
@bp.post("/limits/<int:limit_id>")
@admin_required
def limit_save(limit_id: int | None = None):
    form = request.form
    limit = None
    if limit_id is not None:
        limit = query_one("SELECT * FROM limits WHERE id = ?", (limit_id,))
        if limit is None:
            abort(404)

    def fail(message: str):
        return _limit_form_view(limit, form, message, 400)

    currency = base_currency()

    name = (form.get("name") or "").strip()
    if not name:
        return fail("Give the budget a name — it is what the Telegram message says.")

    # What the budget is about arrives as one field, 'category:7', rather than a
    # kind and an id in two. Two controls would mean four "which one" dropdowns
    # on screen with three of them irrelevant — and with JavaScript off, no way
    # to hide the three. One grouped <select> says the same thing in one tap and
    # cannot be filled in inconsistently.
    scope_type, _, scope_id = (form.get("scope") or "").strip().partition(":")
    scope_id = scope_id or None
    if scope_type not in budgets.SCOPES:
        return fail("Pick what this budget is about.")

    period = (form.get("period") or "").strip()
    if period not in budgets.PERIODS:
        return fail("Pick weekly or monthly.")

    # Household is the one scope with nothing to name, and 001's CHECK pairs the
    # two so tightly that getting it wrong is an IntegrityError rather than a
    # sentence. Hence the branch here.
    if scope_type == "household":
        scope_id = None
    else:
        if not scope_id:
            return fail("Say which one this budget is about.")
        table = {"user": "users", "category": "categories",
                 "account": "accounts", "merchant": "merchants"}[scope_type]
        if query_one(f"SELECT 1 FROM {table} WHERE id = ?", (scope_id,)) is None:
            return fail("That no longer exists — pick another.")

    amount_minor, err = _positive_minor(form.get("amount"), currency, "Budget")
    if err:
        return fail(err)

    try:
        warn_pct = int(form.get("warn_pct") or 80)
    except ValueError:
        return fail("The warning mark is a percentage, like 80.")
    if not 1 <= warn_pct <= 100:
        return fail("The warning mark is between 1 and 100.")

    is_active = 1 if form.get("is_active") else 0
    fields = (name, scope_type, scope_id, period, amount_minor, currency, warn_pct, is_active)

    try:
        if limit is None:
            execute(
                "INSERT INTO limits (name, scope_type, scope_id, period, amount_minor, "
                "currency, warn_pct, is_active, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                fields + (utc_now(),),
            )
        else:
            execute(
                "UPDATE limits SET name=?, scope_type=?, scope_id=?, period=?, "
                "amount_minor=?, currency=?, warn_pct=?, is_active=? WHERE id=?",
                fields + (limit_id,),
            )
            # Lowering a budget mid-month can push it past a mark it already
            # spoke about, and the alert row would keep it quiet. Clearing this
            # period's alerts lets the new number say its piece; older periods
            # stay as they were, because rewriting history is not a correction.
            execute(
                "DELETE FROM limit_alerts WHERE limit_id = ? AND period_key = ?",
                (limit_id, budgets.period_key(period, today_for(g.user["timezone"]))),
            )
    except Exception as exc:
        text = str(exc)
        if text.startswith("limits: "):
            return fail(text[len("limits: "):].strip().capitalize() + ".")
        return fail("That budget could not be saved.")

    flash(f"Saved {name}.", "ok")
    return redirect(url_for("reference.limits"))
