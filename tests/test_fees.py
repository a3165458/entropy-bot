from __future__ import annotations

import pytest

from entropy_bot.errors import ConfigError
from entropy_bot.fees import (
    ALO_TIF_NOTE,
    DEFAULT_FEE_TIER,
    alo_fill_rebate_usd,
    describe_fee_model,
    estimate_fees,
    estimate_from_settings,
    estimate_market_fees,
    hip3_fee_scale,
    rates_for_tier,
)
from entropy_bot.config import Settings


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


def test_default_tier_is_4():
    assert DEFAULT_FEE_TIER == 4
    assert rates_for_tier(4) == (0.00028, 0.00000)


def test_hip3_scale_deployer_fee_1():
    assert hip3_fee_scale(1.0) == 2.0
    assert hip3_fee_scale(0.5) == 1.5


def test_growth_mode_tier4_default_all_in(markets):
    anth = estimate_market_fees(markets["io:ANTH"])
    sndk = estimate_market_fees(markets["io:SNDK"])
    for est in (anth, sndk):
        assert est.tier == 4
        assert est.growth_mode is True
        assert est.scale_if_hip3 == 2.0
        assert est.growth_mode_scale == 0.1
        assert abs(est.taker_pct - 0.0056) < 1e-12
        assert abs(est.maker_pct - 0.0) < 1e-12
        assert ALO_TIF_NOTE in est.summary()
        assert "fee tier 4" in est.summary()
        assert "growthMode=enabled" in est.summary()


def test_estimate_without_growth_tier4():
    est = estimate_fees(deployer_fee_scale=1.0, growth_mode=False, tier=4)
    assert abs(est.taker_pct - 0.056) < 1e-12
    assert abs(est.maker_pct - 0.0) < 1e-12


def test_tier0_growth_mode_still_available():
    est = estimate_fees(deployer_fee_scale=1.0, growth_mode=True, tier=0)
    assert abs(est.taker_pct - 0.009) < 1e-12
    assert abs(est.maker_pct - 0.003) < 1e-12


def test_referral_discounts_taker_only():
    base = estimate_fees(tier=4, referral_discount=0.0)
    disc = estimate_fees(tier=4, referral_discount=0.1)
    assert abs(disc.taker_pct - base.taker_pct * 0.9) < 1e-12
    assert disc.maker_pct == base.maker_pct == 0.0


def test_no_hardcoded_referral_when_unset():
    est = estimate_fees(tier=4)
    assert est.referral_discount == 0.0
    assert abs(est.taker_pct - 0.0056) < 1e-12


def test_maker_rebate_bps_credits_alo_not_assumed():
    plain = estimate_fees(tier=4)
    assert plain.maker_rebate_bps is None
    assert abs(plain.maker_pct - 0.0) < 1e-12
    credited = estimate_fees(tier=4, maker_rebate_bps=0.1)
    assert abs(credited.maker_pct - (-0.001)) < 1e-12
    assert abs(credited.taker_pct - 0.0056) < 1e-12
    assert abs(alo_fill_rebate_usd(10_000, 0.1) - 0.10) < 1e-12
    assert abs(alo_fill_rebate_usd(10_000, -0.2) - 0.20) < 1e-12


def test_settings_drive_estimate(markets):
    settings = _settings(fee_tier=4, referral_discount=0.0, maker_rebate_bps=0.2)
    est = estimate_from_settings(markets["io:SNDK"], settings)
    assert abs(est.taker_pct - 0.0056) < 1e-12
    assert abs(est.maker_pct - (-0.002)) < 1e-12
    banner = describe_fee_model(settings)
    assert "volume tier 4" in banner
    assert "MAKER_REBATE_BPS=0.2 bp" in banner
    assert ALO_TIF_NOTE in banner


def test_unknown_tier_rejected():
    with pytest.raises(ConfigError):
        rates_for_tier(99)
