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
