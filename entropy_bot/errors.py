class EntropyBotError(Exception):
    """Base error for the EntropyIO bot."""


class CoinError(EntropyBotError):
    """Invalid, delisted, or foreign-venue coin name."""


class LiveGuardError(EntropyBotError):
    """Live trading refused because credentials or LIVE=1 are missing."""


class ConfigError(EntropyBotError):
    """Invalid configuration."""


class RateLimited(EntropyBotError):
    """Official API returned 429 or an empty rate-limit body."""


class RequestWeightLimited(RateLimited):
    """Address-level cumulative request weight exhausted.

    Hyperliquid does not restore this cap by waiting. While limited, signed
    writes must back off instead of retrying on every book tick.
    """


def error_text(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, Exception):
        return f"{value} {getattr(value, 'args', ())}"
    try:
        import json

        return json.dumps(value)
    except (TypeError, ValueError):
        return str(value)


def is_weight_limit_error(err: object) -> bool:
    """True for Hyperliquid cumulative request-weight / address-action budget errors."""
    text = error_text(err).lower()
    if not text:
        return False
    return (
        "too many cumulative requests" in text
        or "cumulative request" in text
        or "request weight" in text
    )
