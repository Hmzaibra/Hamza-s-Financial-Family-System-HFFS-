"""Serving, attaching and removing receipt photos.

`uploads/` sits outside `static/` deliberately, so nothing here is reachable
without going through this file. A receipt is a photograph of what someone
bought, where, and often at what time of day — the section 4 rule applies to it
exactly as it applies to the transaction it belongs to, and a folder Flask serves
by filename has no idea who is asking.

So every byte leaves through `serve()`, which loads the attachment's transaction
and asks `can_view()`. A UUID filename is not a permission model: it is only
unguessable until the first time someone forwards a link.
"""

from __future__ import annotations

from flask import Blueprint, abort, flash, g, redirect, request, send_file, url_for

import receipts
from blueprints.auth import login_required
from db import query_one
from visibility import can_edit, can_view

bp = Blueprint("receipts", __name__, url_prefix="/receipts")


def _attachment(attachment_id: int):
    """The attachment and the transaction it hangs off, or 404/403."""
    row = query_one(
        "SELECT a.*, t.user_id, t.is_shared FROM attachments a "
        "JOIN transactions t ON t.id = a.transaction_id WHERE a.id = ?",
        (attachment_id,),
    )
    if row is None:
        abort(404)
    if not can_view(g.user, row):
        # 404 rather than 403: whether a receipt exists on someone else's private
        # purchase is itself part of what section 4 is hiding.
        abort(404)
    return row


def _send(attachment_id: int, column: str):
    row = _attachment(attachment_id)
    try:
        path = receipts.absolute(row[column])
    except receipts.ReceiptError:
        abort(404)
    if not path.is_file():
        # The row outlived its file — a restore from a backup that skipped
        # uploads/, most likely. A broken image is a better answer than a 500.
        abort(404)

    response = send_file(path, mimetype=row["mime"], conditional=True)
    # The bytes never change once written, so the browser may keep them.
    # `private` keeps them out of any shared cache: this is somebody's shopping.
    response.headers["Cache-Control"] = "private, max-age=86400"
    return response


# Two endpoints rather than one view branching on request.path. With both rules
# hung off a single function, `url_for` picks whichever it likes and a template
# asking for the full-size URL can be handed the thumbnail's — which is how the
# gallery ended up requesting /receipts/6/thumb/thumb and drawing broken images
# while a passing check fetched the literal URL by hand.
@bp.get("/<int:attachment_id>")
@login_required
def serve(attachment_id: int):
    return _send(attachment_id, "file_path")


@bp.get("/<int:attachment_id>/thumb")
@login_required
def thumb(attachment_id: int):
    return _send(attachment_id, "thumb_path")


@bp.post("/transactions/<int:txn_id>")
@login_required
def attach(txn_id: int):
    """Add a photo from the edit page.

    The entry form has its own path — a photo taken at the till is posted with
    the entry it belongs to, because the transaction does not exist yet and
    asking someone to save first and photograph second would put a round trip
    in the middle of the fastest screen in the app.
    """
    row = query_one("SELECT id, user_id, is_shared FROM transactions WHERE id = ?", (txn_id,))
    if row is None:
        abort(404)
    if not can_edit(g.user, row):
        abort(403)

    upload = request.files.get("receipt")
    if upload is None or not upload.filename:
        flash("Pick a photo first.", "error")
        return redirect(url_for("ledger.edit", txn_id=txn_id))

    try:
        receipts.store(txn_id, upload)
    except receipts.ReceiptError as exc:
        flash(str(exc), "error")
        return redirect(url_for("ledger.edit", txn_id=txn_id))

    flash("Photo added.", "ok")
    return redirect(url_for("ledger.edit", txn_id=txn_id))


@bp.post("/<int:attachment_id>/delete")
@login_required
def delete(attachment_id: int):
    row = _attachment(attachment_id)
    # Seeing a shared entry's receipt never conferred the right to remove it.
    if not can_edit(g.user, row):
        abort(403)

    txn_id = row["transaction_id"]
    if receipts.remove(attachment_id):
        flash("Photo removed.", "ok")
    else:
        flash("That photo could not be removed.", "error")
    return redirect(url_for("ledger.edit", txn_id=txn_id))
