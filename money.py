"""Money handling. Integers everywhere except at the parsing boundary.

Hard rule 1: money is stored as integers in minor units. `Decimal` appears only
where a human-typed string becomes an integer, and never leaks back out into
arithmetic. There is no function in this module that returns a float.

The exponent table exists because minor units are not universally 1/100. EGP,
EUR and USD are 2, JPY is 0, KWD and BHD are 3. A hardcoded `/ 100` is exactly
the silently-wrong-totals bug hard rule 1 is written to prevent, and the schema
already stores a currency per transaction.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

# ISO 4217 exponents that differ from the default of 2. Everything not listed
# here is assumed to have 2 decimal places, which is true for the large majority
# of currencies including every one this household is likely to touch.
_EXPONENTS: dict[str, int] = {
    "BHD": 3, "BIF": 0, "CLP": 0, "DJF": 0, "GNF": 0, "IQD": 3, "ISK": 0,
    "JOD": 3, "JPY": 0, "KMF": 0, "KRW": 0, "KWD": 3, "LYD": 3, "OMR": 3,
    "PYG": 0, "RWF": 0, "TND": 3, "UGX": 0, "UYI": 0, "VND": 0, "VUV": 0,
    "XAF": 0, "XOF": 0, "XPF": 0,
}

# Symbols are cosmetic; the currency code is the source of truth.
_SYMBOLS: dict[str, str] = {
    "EGP": "E£", "EUR": "€", "USD": "$", "GBP": "£", "AED": "AED", "SAR": "SAR",
}

# Also cosmetic, and deliberately never load-bearing. A flag is a pair of
# regional-indicator characters, which renders as a flag on iOS and Android and
# as two boxed letters on Windows, which has no flag font. That degradation is
# fine — the currency code is always printed next to it — but it is the reason
# nothing here is ever the only way to tell two currencies apart.
_FLAGS: dict[str, str] = {
    "EGP": "🇪🇬", "EUR": "🇪🇺", "USD": "🇺🇸", "GBP": "🇬🇧", "AED": "🇦🇪", "SAR": "🇸🇦",
}


class MoneyError(ValueError):
    """Raised when a human-supplied amount cannot be trusted."""


def exponent(currency: str) -> int:
    return _EXPONENTS.get((currency or "").upper(), 2)


def symbol(currency: str) -> str:
    code = (currency or "").upper()
    return _SYMBOLS.get(code, code)


def flag(currency: str) -> str:
    """The currency's flag, or an empty string for one without a mapping."""
    return _FLAGS.get((currency or "").upper(), "")


def parse_to_minor(raw: str | Decimal | int, currency: str) -> int:
    """Turn what someone typed into an integer number of minor units.

    This is the only boundary where Decimal is allowed. Rejects negatives
    outright: hard rule 2 says amounts are positive and direction carries the
    sign, so a minus sign here is a mistake worth surfacing, not absorbing.
    """
    if isinstance(raw, float):
        # Guarding the type rather than trusting the caller: a float that reached
        # here has already lost precision, and accepting it would make hard
        # rule 1 unenforceable from this side.
        raise MoneyError("amounts must not be parsed from float")

    if isinstance(raw, str):
        cleaned = raw.strip().replace(",", "").replace("٫", ".").replace(" ", "")
        if not cleaned:
            raise MoneyError("enter an amount")
    else:
        cleaned = raw

    try:
        value = Decimal(cleaned)
    except (InvalidOperation, TypeError):
        raise MoneyError("that doesn't look like an amount") from None

    if not value.is_finite():
        raise MoneyError("that doesn't look like an amount")
    if value < 0:
        raise MoneyError("amounts are always positive — pick a direction instead")
    if value == 0:
        raise MoneyError("amount must be more than zero")

    quantum = Decimal(1).scaleb(-exponent(currency))
    minor = int((value.quantize(quantum, rounding=ROUND_HALF_UP)) / quantum)

    if minor <= 0:
        raise MoneyError("amount must be more than zero")
    if minor > 2**62:
        raise MoneyError("that amount is implausibly large")
    return minor


def format_minor(minor: int, currency: str, *, with_symbol: bool = False) -> str:
    """Render minor units for display. Integer maths only, no float division."""
    if minor is None:
        return ""
    exp = exponent(currency)
    negative = minor < 0
    minor = abs(int(minor))

    if exp == 0:
        body = f"{minor:,}"
    else:
        scale = 10**exp
        whole, frac = divmod(minor, scale)
        body = f"{whole:,}.{frac:0{exp}d}"

    if negative:
        body = f"-{body}"
    return f"{symbol(currency)} {body}" if with_symbol else body


def convert_to_base(minor: int, rate: float | None) -> int:
    """Convert a stored amount into base currency using its captured rate.

    Deliberately Python rather than SQL. `SUM(amount_minor * fx_rate_to_base)`
    inside SQLite is float arithmetic on money through the back door, which hard
    rule 1 exists to forbid; the rate crosses into Decimal here and the result
    lands back on an integer before it is summed anywhere.
    """
    if rate is None:
        return int(minor)
    dec_rate = Decimal(str(rate))
    if dec_rate <= 0:
        raise MoneyError("fx rate must be positive")
    return int((Decimal(int(minor)) * dec_rate).quantize(Decimal(1), rounding=ROUND_HALF_UP))
