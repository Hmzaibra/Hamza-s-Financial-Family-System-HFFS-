"""Sending a message to Telegram. The second and last thing that leaves the box.

Same shape as `fx.py` and for the same reason: this is called from a cron
command, never from a request. Invariant 7 is not a style preference — an entry
form that waits on api.telegram.org is an entry form that hangs in a supermarket
basement, and a budget warning is worth exactly nothing compared to being able
to log the purchase that triggered it.

No dependency for this. The Bot API is one form-encoded POST, and adding
`requests` to a Raspberry Pi venv to avoid twenty lines of urllib is a trade in
the wrong direction.

Setting it up, once:

  1. Message @BotFather on Telegram, /newbot, and copy the token it gives you
     into TELEGRAM_BOT_TOKEN in .env. The token is not a setting in the database
     — it is a credential, and credentials live in the environment.
  2. Each person who should get alerts sends the bot any message.
  3. `flask --app app telegram-chats` lists who has written in, with their chat
     id. Paste it into Setup → People.

Step 2 is not optional and is not this app's choice: Telegram will not let a bot
message someone who has never messaged it. That is a spam rule, and it is why
there is no way to add a family member to alerts without them doing something.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request

API = "https://api.telegram.org/bot{token}/{method}"

# Long enough for a phone tethered to a slow line, short enough that a cron run
# does not sit for a minute per recipient if Telegram is having a bad day.
TIMEOUT = 15.0


class TelegramError(RuntimeError):
    """A message did not go out. Never fatal — the sweep reports and carries on."""


def _call(token: str, method: str, params: dict):
    """POST to one Bot API method. Returns whatever `result` holds — sendMessage
    answers with an object, getUpdates with a list."""
    if not token:
        raise TelegramError("TELEGRAM_BOT_TOKEN is not set")

    body = urllib.parse.urlencode(params).encode("utf-8")
    request = urllib.request.Request(
        API.format(token=token, method=method),
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        # Telegram puts the useful part in the body of a 4xx — "chat not found",
        # "bot was blocked by the user". The status code alone says nothing.
        try:
            detail = json.loads(exc.read().decode("utf-8")).get("description", "")
        except Exception:
            detail = ""
        raise TelegramError(detail or f"telegram returned {exc.code}") from None
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise TelegramError(f"could not reach telegram: {exc}") from None
    except json.JSONDecodeError:
        raise TelegramError("telegram did not return JSON") from None

    if not payload.get("ok"):
        raise TelegramError(payload.get("description") or "telegram refused the message")
    result = payload.get("result")
    return result if result is not None else {}


def send(token: str, chat_id: str, text: str) -> None:
    """One message to one chat.

    Deliberately no parse_mode. Markdown would mean escaping every underscore in
    a merchant name, and a warning that arrives with visible backslashes in it —
    or does not arrive at all because the escaping was wrong — is worse than one
    in plain text.
    """
    _call(token, "sendMessage", {
        "chat_id": chat_id,
        "text": text,
        "disable_web_page_preview": "true",
    })


def recent_chats(token: str) -> list[dict]:
    """Who has written to the bot lately, for `flask telegram-chats`.

    getUpdates only reaches back about 24 hours, which is fine for what this is
    for: someone sends the bot a message, then runs this to find out what number
    to paste in. It is a setup aid, not a source of truth.
    """
    seen: dict[str, dict] = {}
    for update in _call(token, "getUpdates", {"limit": "100"}) or []:
        message = update.get("message") or update.get("edited_message") or {}
        chat = message.get("chat") or {}
        if not chat.get("id"):
            continue
        name = " ".join(
            part for part in (chat.get("first_name"), chat.get("last_name")) if part
        ) or chat.get("title") or chat.get("username") or "(no name)"
        seen[str(chat["id"])] = {"chat_id": str(chat["id"]), "name": name}
    return list(seen.values())
