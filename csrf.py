"""CSRF protection, without adding a dependency.

SameSite=Lax already stops a cross-site form POST in current browsers, which is
most of the threat. It is not on its own something to bet a money app on: it
does nothing for a same-site injection, and it is one browser-default change
away from being weaker than assumed. Thirty lines buys the second layer.

Applied to every unsafe method with no exemption list — an exemption list is how
one endpoint ends up unprotected two phases from now.
"""

from __future__ import annotations

import secrets

from flask import abort, request, session
from markupsafe import Markup

FIELD = "_csrf"
HEADER = "X-CSRF-Token"
SAFE_METHODS = {"GET", "HEAD", "OPTIONS", "TRACE"}


def csrf_token() -> str:
    token = session.get(FIELD)
    if not token:
        token = secrets.token_urlsafe(32)
        session[FIELD] = token
    return token


def csrf_input() -> Markup:
    """Hidden field for templates: {{ csrf_input() }} inside every form."""
    return Markup(f'<input type="hidden" name="{FIELD}" value="{csrf_token()}">')


def protect() -> None:
    if request.method in SAFE_METHODS:
        return

    sent = request.form.get(FIELD) or request.headers.get(HEADER, "")
    expected = session.get(FIELD, "")

    # compare_digest on two empty strings is True, so the emptiness of the
    # expected token has to be checked separately or a session with no token
    # would accept a request with no token.
    if not expected or not sent or not secrets.compare_digest(sent, expected):
        abort(400, description="Your form session expired. Go back and try again.")


def init_app(app) -> None:
    app.before_request(protect)
    app.jinja_env.globals["csrf_token"] = csrf_token
    app.jinja_env.globals["csrf_input"] = csrf_input
