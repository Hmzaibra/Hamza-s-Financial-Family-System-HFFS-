"""Cached exchange rates.

The only code in this project that reaches the public internet, and it does so
from a cron job rather than from a request. That separation is the whole design:
a rate lookup that blocks the entry form would trade the app's one hard promise —
that it works with the internet unplugged — for a number the form can perfectly
well ask a human for.

What this buys is a *default*. The rate stored against a transaction is still the
one captured at entry, because the rate on the day of purchase is unrecoverable
afterwards and is the only correct one. This module just means the box arrives
with a plausible number in it instead of empty.

    flask --app app fetch-rates          refresh if the cache is over a week old
    flask --app app fetch-rates --force  refresh regardless

Run it daily from cron; the age check makes all but one run a week a no-op, so
the schedule can be generous without hammering anyone's API.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from datetime import datetime, timezone

from db import UTC_FORMAT, execute, get_db, query, utc_now

# Free, no key, no account. The URL is configurable precisely because a provider
# that is free today may not be in two years, and swapping it should not mean
# editing code on a Raspberry Pi over SSH.
DEFAULT_SOURCE = "https://open.er-api.com/v6/latest/{base}"

# The currencies the entry form offers. Fetching the provider's full list would
# be a few hundred rows nobody looks at.
WANTED = ("EGP", "EUR", "USD", "GBP", "AED", "SAR")

DEFAULT_MAX_AGE_DAYS = 7


class RateError(RuntimeError):
    """The rates could not be refreshed. Never fatal — the cache stays as it was."""


def cached(base: str) -> dict[str, dict]:
    """Every known rate into `base`, keyed by currency code."""
    rows = query(
        "SELECT currency, rate_to_base, fetched_at FROM fx_rates WHERE base = ?", (base,)
    )
    return {
        row["currency"]: {
            "rate": row["rate_to_base"],
            "fetched_at": row["fetched_at"],
            "age_days": age_days(row["fetched_at"]),
        }
        for row in rows
    }


def age_days(fetched_at: str) -> int | None:
    try:
        stamp = datetime.strptime(fetched_at, UTC_FORMAT).replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None
    return max(0, (datetime.now(timezone.utc) - stamp).days)


def stale(base: str, max_age_days: int = DEFAULT_MAX_AGE_DAYS) -> bool:
    """True when nothing has been fetched, or the freshest row is old enough."""
    rows = cached(base)
    if not rows:
        return True
    ages = [r["age_days"] for r in rows.values() if r["age_days"] is not None]
    return not ages or min(ages) >= max_age_days


def fetch(base: str, url_template: str = DEFAULT_SOURCE, timeout: float = 15.0) -> dict[str, float]:
    """Ask the provider what `base` is worth, and invert into rate-to-base.

    Providers quote "1 base = X foreign". A transaction stores the opposite —
    how much base one unit of the foreign currency is worth — so the inversion
    happens here rather than being left for a reader to spot at a call site.
    """
    url = url_template.format(base=base)
    request = urllib.request.Request(url, headers={"User-Agent": "family-expense-tracker"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise RateError(f"could not reach {url}: {exc}") from None
    except json.JSONDecodeError:
        raise RateError(f"{url} did not return JSON") from None

    quoted = payload.get("rates") or payload.get("conversion_rates")
    if not isinstance(quoted, dict):
        raise RateError(f"{url} returned no rates")

    rates: dict[str, float] = {}
    for code in WANTED:
        if code == base:
            continue
        value = quoted.get(code)
        try:
            value = float(value)
        except (TypeError, ValueError):
            continue
        if value > 0:
            rates[code] = 1.0 / value

    if not rates:
        raise RateError(f"{url} carried none of the currencies this app uses")
    return rates


def store(base: str, rates: dict[str, float], source: str) -> int:
    now = utc_now()
    conn = get_db()
    for currency, rate in rates.items():
        conn.execute(
            "INSERT INTO fx_rates (base, currency, rate_to_base, fetched_at, source) "
            "VALUES (?,?,?,?,?) "
            "ON CONFLICT(base, currency) DO UPDATE SET "
            "  rate_to_base = excluded.rate_to_base, fetched_at = excluded.fetched_at, "
            "  source = excluded.source",
            (base, currency, rate, now, source),
        )
    conn.commit()
    return len(rates)


def refresh(
    base: str,
    url_template: str = DEFAULT_SOURCE,
    max_age_days: int = DEFAULT_MAX_AGE_DAYS,
    force: bool = False,
) -> tuple[int, str]:
    """Refresh if stale. Returns (rows written, a line worth logging)."""
    if not force and not stale(base, max_age_days):
        return 0, f"rates for {base} are under {max_age_days} days old — nothing to do"

    rates = fetch(base, url_template)
    written = store(base, rates, url_template.format(base=base))
    return written, f"stored {written} rates into {base}"


def clear_unused(base: str) -> None:
    """Drop rows for a base the app no longer uses.

    Changing the household's base currency is rare and deliberate, but leaving
    the old base's rows behind would mean a stale number surfacing the one time
    someone switches back.
    """
    execute("DELETE FROM fx_rates WHERE base <> ?", (base,))
