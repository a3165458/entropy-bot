from __future__ import annotations

from entropy_bot.fees import estimate_fees, estimate_market_fees, hip3_fee_scale


def test_hip3_scale_deployer_fee_1():
    assert hip3_fee_scale(1.0) == 2.0
    assert hip3_fee_scale(0.5) == 1.5


def test_growth_mode_tier0_matches_docs(markets):
    anth = estimate_market_fees(markets["io:ANTH"])
    sndk = estimate_market_fees(markets["io:SNDK"])
    for est in (anth, sndk):
        assert est.growth_mode is True
        assert est.scale_if_hip3 == 2.0
        assert est.growth_mode_scale == 0.1
        assert abs(est.taker_pct - 0.009) < 1e-12
        assert abs(est.maker_pct - 0.003) < 1e-12


def test_estimate_without_growth_is_10x():
    est = estimate_fees(deployer_fee_scale=1.0, growth_mode=False)
    assert abs(est.taker_pct - 0.09) < 1e-12
    assert abs(est.maker_pct - 0.03) < 1e-12
