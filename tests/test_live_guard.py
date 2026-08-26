from __future__ import annotations

import pytest

from entropy_bot.cli import main
from entropy_bot.config import Settings, live_notional, require_live
from entropy_bot.errors import LiveGuardError
from entropy_bot.live import RestSlot, apply_open_orders, deadman_deadline_ms, empty_rests


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
    assert settings.entropy_tier == 4
    assert settings.entropy_self_rebate == 2.0
    assert settings.entropy_referral_reward == 1.0
    assert settings.entropy_referred_user_benefit == 0.0
    assert settings.min_replace_s == 12.0


def test_fee_env_optional_layers(monkeypatch):
    from entropy_bot.config import load_settings

    monkeypatch.setenv("COINS", "io:ANTH,io:SNDK")
    monkeypatch.setenv("FEE_TIER", "4")
    monkeypatch.setenv("REFERRAL_DISCOUNT", "0.04")
    monkeypatch.setenv("MAKER_REBATE_BPS", "0.3")
    monkeypatch.setenv("ENTROPY_TIER", "4")
    monkeypatch.setenv("ENTROPY_SELF_REBATE", "2.0")
    monkeypatch.setenv("ENTROPY_REFERRAL_REWARD", "1.0")
    monkeypatch.setenv("ENTROPY_REFERRED_USER_BENEFIT", "0")
    settings = load_settings()
    assert settings.fee_tier == 4
    assert settings.referral_discount == 0.04
    assert settings.maker_rebate_bps == 0.3
    assert settings.coins == ("io:ANTH", "io:SNDK")
    assert settings.entropy_tier == 4
    assert settings.entropy_self_rebate == 2.0
    assert settings.entropy_referral_reward == 1.0
    assert settings.entropy_referred_user_benefit == 0.0


def test_min_replace_s_from_env(monkeypatch):
    from entropy_bot.config import load_settings

    monkeypatch.setenv("COINS", "io:ANTH")
    monkeypatch.setenv("MIN_REPLACE_S", "15")
    settings = load_settings()
    assert settings.min_replace_s == 15.0
    assert settings.coins == ("io:ANTH",)


def test_env_rejects_xyz_coins(monkeypatch):
    from entropy_bot.config import load_settings
    from entropy_bot.errors import CoinError

    monkeypatch.setenv("COINS", "xyz:SNDK")
    with pytest.raises(CoinError):
        load_settings()


def test_live_notional_defaults_to_fifty():
    settings = _settings(quote_notional_usd=50.0)
    assert live_notional(settings) == 50.0
    assert live_notional(_settings(quote_notional_usd=8.0)) == 10.0


def test_open_orders_null_does_not_wipe_cache(markets):
    rests = empty_rests(("io:ANTH", "io:SNDK"))
    extras = {"io:ANTH": [], "io:SNDK": []}
    rests["io:ANTH"]["B"] = RestSlot(oid=3_000_000_000, cloid="0x45424f54ab", px=1985.8)
    kept, kept_ex = apply_open_orders(rests, extras, None, markets)
    assert kept["io:ANTH"]["B"].oid == 3_000_000_000
    assert kept_ex is extras
    rebuilt, extras2 = apply_open_orders(
        rests,
        extras,
        [{"coin": "io:ANTH", "oid": 3_000_000_001, "side": "B", "limitPx": "1985.8"}],
        markets,
    )
    assert rebuilt["io:ANTH"]["B"].oid == 3_000_000_001
    assert extras2["io:ANTH"] == []


def test_deadman_clamped_to_at_least_five_seconds():
    now = 1_700_000_000_000
    assert deadman_deadline_ms(20_000, now_ms=now) == now + 20_000
    assert deadman_deadline_ms(1_000, now_ms=now) == now + 6_000
