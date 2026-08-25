from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

from entropy_bot.coins import ALLOWED_COINS, validate_coin_list
from entropy_bot.errors import ConfigError, LiveGuardError
from entropy_bot.fees import (
    DEFAULT_ENTROPY_REFERRAL_REWARD,
    DEFAULT_ENTROPY_REFERRED_USER_BENEFIT,
    DEFAULT_ENTROPY_SELF_REBATE,
    DEFAULT_ENTROPY_TIER,
    DEFAULT_FEE_TIER,
    entropy_program,
    rates_for_tier,
)

DEFAULT_API_URL = "https://api.hyperliquid.xyz"
DEFAULT_WS_URL = "wss://api.hyperliquid.xyz/ws"
DEFAULT_NOTIONAL = 50.0
DEFAULT_OFFSET_TICKS = 2
DEFAULT_MAX_LEVERAGE = 2
MIN_ORDER_USD = 10.0


@dataclass(frozen=True)
class Settings:
    live: bool
    private_key: str | None
    account: str | None
    coins: tuple[str, ...]
    quote_notional_usd: float
    quote_offset_ticks: int
    max_leverage: int
    api_url: str
    ws_url: str
    fee_tier: int = DEFAULT_FEE_TIER
    referral_discount: float = 0.0
    maker_rebate_bps: float | None = None
    entropy_tier: int = DEFAULT_ENTROPY_TIER
    entropy_self_rebate: float = DEFAULT_ENTROPY_SELF_REBATE
    entropy_referral_reward: float = DEFAULT_ENTROPY_REFERRAL_REWARD
    entropy_referred_user_benefit: float = DEFAULT_ENTROPY_REFERRED_USER_BENEFIT

    @property
    def paper(self) -> bool:
        return not self.live


def _truthy(value: str | None) -> bool:
    if value is None:
        return False
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _optional_env(name: str) -> str | None:
    value = os.environ.get(name, "").strip()
    return value or None


def load_settings(dotenv_path: str | Path | None = None) -> Settings:
    load_dotenv(dotenv_path, override=False)
    coins_raw = os.environ.get("COINS", ",".join(ALLOWED_COINS))
    coins = validate_coin_list(part.strip() for part in coins_raw.split(",") if part.strip())
    try:
        notional = float(os.environ.get("QUOTE_NOTIONAL_USD", DEFAULT_NOTIONAL))
        offset = int(os.environ.get("QUOTE_OFFSET_TICKS", DEFAULT_OFFSET_TICKS))
        max_lev = int(os.environ.get("MAX_LEVERAGE", DEFAULT_MAX_LEVERAGE))
        fee_tier = int(os.environ.get("FEE_TIER", DEFAULT_FEE_TIER))
        referral = float(os.environ.get("REFERRAL_DISCOUNT", "0") or 0)
        rebate_raw = _optional_env("MAKER_REBATE_BPS")
        rebate_bps = float(rebate_raw) if rebate_raw is not None else None
        entropy_tier = int(os.environ.get("ENTROPY_TIER", DEFAULT_ENTROPY_TIER))
        self_rebate_raw = _optional_env("ENTROPY_SELF_REBATE")
        entropy_self_rebate = (
            float(self_rebate_raw) if self_rebate_raw is not None else DEFAULT_ENTROPY_SELF_REBATE
        )
        reward_raw = _optional_env("ENTROPY_REFERRAL_REWARD")
        entropy_referral_reward = (
            float(reward_raw) if reward_raw is not None else DEFAULT_ENTROPY_REFERRAL_REWARD
        )
        referred_raw = _optional_env("ENTROPY_REFERRED_USER_BENEFIT")
        entropy_referred = (
            float(referred_raw) if referred_raw is not None else DEFAULT_ENTROPY_REFERRED_USER_BENEFIT
        )
    except ValueError as exc:
        raise ConfigError(f"invalid numeric config: {exc}") from exc
    if notional <= 0 or offset < 0 or max_lev < 1:
        raise ConfigError("QUOTE_NOTIONAL_USD, QUOTE_OFFSET_TICKS, MAX_LEVERAGE must be positive")
    rates_for_tier(fee_tier)
    entropy_program(entropy_tier)
    if not 0.0 <= referral < 1.0:
        raise ConfigError("REFERRAL_DISCOUNT must be in [0, 1)")
    if entropy_self_rebate < 0 or entropy_referral_reward < 0 or entropy_referred < 0:
        raise ConfigError("Entropy rebate rates must be >= 0")
    api_url = _optional_env("HYPERLIQUID_API_URL") or DEFAULT_API_URL
    ws_url = _optional_env("HYPERLIQUID_WS_URL") or DEFAULT_WS_URL
    if "hyperliquid.xyz" not in api_url or "hyperliquid.xyz" not in ws_url:
        raise ConfigError("only official Hyperliquid HTTP/WS endpoints are allowed")
    return Settings(
        live=_truthy(os.environ.get("LIVE")),
        private_key=_optional_env("HYPERLIQUID_PRIVATE_KEY"),
        account=_optional_env("HYPERLIQUID_ACCOUNT"),
        coins=coins,
        quote_notional_usd=notional,
        quote_offset_ticks=offset,
        max_leverage=max_lev,
        api_url=api_url.rstrip("/"),
        ws_url=ws_url,
        fee_tier=fee_tier,
        referral_discount=referral,
        maker_rebate_bps=rebate_bps,
        entropy_tier=entropy_tier,
        entropy_self_rebate=entropy_self_rebate,
        entropy_referral_reward=entropy_referral_reward,
        entropy_referred_user_benefit=entropy_referred,
    )


def require_live(settings: Settings) -> None:
    if not settings.live:
        raise LiveGuardError("live trading refused: set LIVE=1 (paper is the default)")
    if not settings.private_key:
        raise LiveGuardError("live trading refused: HYPERLIQUID_PRIVATE_KEY is not set")


def require_signer(settings: Settings) -> None:
    if not settings.private_key:
        raise LiveGuardError("signing refused: HYPERLIQUID_PRIVATE_KEY is not set")


def live_notional(settings: Settings) -> float:
    """Default ~$50/side, never below the $10 exchange minimum."""
    return max(MIN_ORDER_USD, settings.quote_notional_usd)
