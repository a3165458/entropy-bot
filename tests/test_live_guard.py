from __future__ import annotations

import pytest

from entropy_bot.cli import main
from entropy_bot.config import Settings, require_live
from entropy_bot.errors import LiveGuardError


def _settings(**kwargs) -> Settings:
    base = dict(
        live=False,
        private_key=None,
        account=None,
        coins=("io:ANTH", "io:SNDK"),
        quote_notional_usd=50.0,
        quote_offset_ticks=2,
        max_leverage=2,
        api_url="https://api.hyperliquid.xyz",
        ws_url="wss://api.hyperliquid.xyz/ws",
    )
    base.update(kwargs)
    return Settings(**base)


def test_require_live_needs_flag_and_key():
    with pytest.raises(LiveGuardError, match="LIVE=1"):
        require_live(_settings(live=False, private_key="0xabc"))
    with pytest.raises(LiveGuardError, match="HYPERLIQUID_PRIVATE_KEY"):
        require_live(_settings(live=True, private_key=None))
    require_live(_settings(live=True, private_key="0x" + "ab" * 32))


def test_cli_live_without_key(monkeypatch):
    monkeypatch.delenv("HYPERLIQUID_PRIVATE_KEY", raising=False)
    monkeypatch.setenv("LIVE", "1")
    monkeypatch.setenv("COINS", "io:ANTH,io:SNDK")
    assert main(["live", "--seconds", "0.1"]) == 2


def test_cli_live_without_live_flag(monkeypatch):
    monkeypatch.setenv("LIVE", "0")
    monkeypatch.setenv("HYPERLIQUID_PRIVATE_KEY", "0x" + "ab" * 32)
    assert main(["live"]) == 2


def test_paper_does_not_require_key(monkeypatch):
    from entropy_bot.config import load_settings

    monkeypatch.delenv("HYPERLIQUID_PRIVATE_KEY", raising=False)
    monkeypatch.setenv("LIVE", "0")
    monkeypatch.setenv("COINS", "io:ANTH,io:SNDK")
    settings = load_settings()
    assert settings.private_key is None
    assert settings.paper is True
    assert settings.coins == ("io:ANTH", "io:SNDK")
    assert settings.fee_tier == 4
    assert settings.referral_discount == 0.0
    assert settings.maker_rebate_bps is None


def test_fee_env_optional_layers(monkeypatch):
    from entropy_bot.config import load_settings

    monkeypatch.setenv("COINS", "io:ANTH,io:SNDK")
    monkeypatch.setenv("FEE_TIER", "4")
    monkeypatch.setenv("REFERRAL_DISCOUNT", "0.04")
    monkeypatch.setenv("MAKER_REBATE_BPS", "0.3")
    settings = load_settings()
    assert settings.fee_tier == 4
    assert settings.referral_discount == 0.04
    assert settings.maker_rebate_bps == 0.3
    assert settings.coins == ("io:ANTH", "io:SNDK")


def test_env_rejects_xyz_coins(monkeypatch):
    from entropy_bot.config import load_settings
    from entropy_bot.errors import CoinError

    monkeypatch.setenv("COINS", "xyz:SNDK")
    with pytest.raises(CoinError):
        load_settings()
