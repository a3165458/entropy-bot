from __future__ import annotations

import pytest

from entropy_bot.config import Settings
from entropy_bot.errors import ConfigError
from entropy_bot.fees import (
    ALO_TIF_NOTE,
    DEFAULT_FEE_TIER,
    alo_fill_rebate_usd,
    describe_fee_model,
    entropy_deployer_share,
    estimate_fees,
    estimate_from_settings,
    estimate_market_fees,
    hip3_fee_scale,
    rates_for_tier,
    worked_fee_example,
)


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


def test_default_hl_tier_is_4():
    assert DEFAULT_FEE_TIER == 4
    assert rates_for_tier(4) == (0.00028, 0.00000)


def test_hip3_scale_and_entropy_share():
    assert hip3_fee_scale(1.0) == 2.0
    assert hip3_fee_scale(0.5) == 1.5
    assert entropy_deployer_share(1.0) == 0.5
    assert entropy_deployer_share(2.0) == 0.5
    assert abs(entropy_deployer_share(0.5) - (0.5 / 1.5)) < 1e-12


def test_docs_100_fee_partner_self_rebate_nets_zero():
    """$100 total fee, 50 HL / 50 Entropy; 200% of Entropy's $50 = $100; net $0."""
    half = worked_fee_example(100.0, self_rebate=0.5, referred_user_benefit=0.0)
    assert half.hyperliquid == 50
    assert half.entropy == 50
    assert half.self_rebate == 25  # 50% of Entropy share, not 50% of $100
    assert half.net == 75

    partner = worked_fee_example(100.0, self_rebate=2.0, referred_user_benefit=0.0)
    assert partner.hyperliquid == 50
    assert partner.entropy == 50
    assert partner.self_rebate == 100  # 200% of Entropy's $50
    assert partner.net == 0
    # 优惠 100% is not "100% off the full $100 Hyperliquid fee".
    assert partner.hyperliquid == 50
    assert partner.self_rebate == partner.entropy * 2.0


def test_docs_100_fee_does_not_apply_youhui_to_own_fills():
    own = worked_fee_example(100.0, self_rebate=2.0, referred_user_benefit=0.0)
    invited = worked_fee_example(100.0, self_rebate=2.0, referred_user_benefit=1.0)
    assert own.net == 0
    assert invited.referred_user_benefit == 50
    assert invited.net == -50


def test_growth_mode_tier4_partner_net_taker_zero(markets):
    anth = estimate_market_fees(markets["io:ANTH"])
    sndk = estimate_market_fees(markets["io:SNDK"])
    for est in (anth, sndk):
        assert est.tier == 4
        assert est.entropy_tier == 4
        assert est.entropy_tier_name == "Partner"
        assert est.growth_mode is True
        assert est.scale_if_hip3 == 2.0
        assert est.entropy_share == 0.5
        assert est.entropy_self_rebate == 2.0
        assert abs(est.gross_taker_pct - 0.0056) < 1e-12
        assert abs(est.self_rebate_taker_pct - 0.0056) < 1e-12
        assert abs(est.taker_pct - 0.0) < 1e-12
        assert abs(est.gross_maker_pct - 0.0) < 1e-12
        assert abs(est.maker_pct - 0.0) < 1e-12
        assert est.referred_benefit_taker_pct == 0.0
        text = est.summary()
        assert "Entropy Partner T4" in text
        assert "self rebate 200% of deployer share" in text
        assert "gross taker 0.0056%" in text
        assert "net 0.0000%" in text
        assert ALO_TIF_NOTE in text
        assert "优惠/referred-user benefit not applied to own fills" in text


def test_youhui_not_applied_unless_referred_benefit_set():
    default = estimate_fees(tier=4, entropy_referral_reward=1.0, entropy_referred_user_benefit=0.0)
    assert default.entropy_referral_reward == 1.0
    assert abs(default.taker_pct - 0.0) < 1e-12
    extra = estimate_fees(tier=4, entropy_referred_user_benefit=1.0)
    # extra 100% of Entropy's 50% share of 0.0056% = 0.0028%
    assert abs(extra.referred_benefit_taker_pct - 0.0028) < 1e-12
    assert abs(extra.taker_pct - (-0.0028)) < 1e-12


def test_estimate_without_growth_still_nets_zero_at_partner():
    est = estimate_fees(deployer_fee_scale=1.0, growth_mode=False, tier=4)
    assert abs(est.gross_taker_pct - 0.056) < 1e-12
    assert abs(est.taker_pct - 0.0) < 1e-12
    assert abs(est.maker_pct - 0.0) < 1e-12


def test_tier0_gross_still_available():
    est = estimate_fees(deployer_fee_scale=1.0, growth_mode=True, tier=0, entropy_self_rebate=0.0)
    assert abs(est.gross_taker_pct - 0.009) < 1e-12
    assert abs(est.gross_maker_pct - 0.003) < 1e-12
    assert abs(est.taker_pct - 0.009) < 1e-12


def test_hl_referral_discounts_gross_taker_only():
    base = estimate_fees(tier=4, referral_discount=0.0, entropy_self_rebate=0.0)
    disc = estimate_fees(tier=4, referral_discount=0.1, entropy_self_rebate=0.0)
    assert abs(disc.gross_taker_pct - base.gross_taker_pct * 0.9) < 1e-12
    assert disc.gross_maker_pct == base.gross_maker_pct == 0.0


def test_maker_rebate_bps_credits_alo_not_assumed():
    plain = estimate_fees(tier=4)
    assert plain.maker_rebate_bps is None
    assert abs(plain.maker_pct - 0.0) < 1e-12
    credited = estimate_fees(tier=4, maker_rebate_bps=0.1)
    assert abs(credited.gross_maker_pct - (-0.001)) < 1e-12
    assert abs(credited.maker_pct - (-0.001)) < 1e-12
    assert abs(credited.taker_pct - 0.0) < 1e-12
    assert abs(credited.gross_taker_pct - 0.0056) < 1e-12
    assert abs(alo_fill_rebate_usd(10_000, 0.1) - 0.10) < 1e-12


def test_settings_drive_estimate(markets):
    settings = _settings(fee_tier=4, maker_rebate_bps=0.2)
    est = estimate_from_settings(markets["io:SNDK"], settings)
    assert abs(est.gross_taker_pct - 0.0056) < 1e-12
    assert abs(est.taker_pct - 0.0) < 1e-12
    assert abs(est.maker_pct - (-0.002)) < 1e-12
    banner = describe_fee_model(settings)
    assert "Entropy Partner T4" in banner
    assert "self rebate 200% of deployer share" in banner
    assert "HL volume tier 4" in banner
    assert ALO_TIF_NOTE in banner


def test_unknown_tier_rejected():
    with pytest.raises(ConfigError):
        rates_for_tier(99)
    with pytest.raises(ConfigError):
        estimate_fees(entropy_tier=99)
