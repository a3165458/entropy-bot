"""HIP-3 growth-mode fee estimate using official Hyperliquid volume tiers.

Default model is **volume tier 4** (14d weighted volume > $500M):

    official perp base: taker 0.028% / maker 0.000%
    deployerFeeScale=1 → scaleIfHip3=2
    growthMode enabled → ×0.1
    all-in: taker 0.0056% / maker 0.000%

Referral discount (env ``REFERRAL_DISCOUNT``) multiplies **taker only**:
``taker * (1 - discount)``. It is not hardcoded — default is 0.

Optional ``MAKER_REBATE_BPS`` credits ALO / maker fills. Official maker-share
tiers are -0.001% / -0.002% / -0.003% (0.1 / 0.2 / 0.3 bp) at 0.5% / 1.5% /
3.0% of 14d maker volume. Unset → assume maker fee 0.
"""

from __future__ import annotations

from dataclasses import dataclass

from entropy_bot.coins import Market
from entropy_bot.errors import ConfigError

# Official validator-operated perp volume tiers (fraction of notional).
PERP_VOLUME_TIERS: dict[int, tuple[float, float]] = {
    0: (0.00045, 0.00015),
    1: (0.00040, 0.00012),
    2: (0.00035, 0.00008),
    3: (0.00030, 0.00004),
    4: (0.00028, 0.00000),
    5: (0.00026, 0.00000),
    6: (0.00024, 0.00000),
}
DEFAULT_FEE_TIER = 4

# Official maker-volume rebate table, in basis points of notional (1 bp = 0.01%).
# Applied only when MAKER_REBATE_BPS is set — not assumed.
MAKER_SHARE_REBATE_BPS: dict[float, float] = {
    0.5: 0.1,  # >0.5% of 14d maker volume → -0.001%
    1.5: 0.2,  # >1.5% → -0.002%
    3.0: 0.3,  # >3.0% → -0.003%
}

ALO_TIF_NOTE = "default TIF=ALO (maker/rebate side only; never IOC/market)"


@dataclass(frozen=True)
class FeeEstimate:
    taker_pct: float
    maker_pct: float
    scale_if_hip3: float
    growth_mode_scale: float
    deployer_fee_scale: float
    growth_mode: bool
    tier: int
    referral_discount: float = 0.0
    maker_rebate_bps: float | None = None
    base_taker_pct: float = 0.028
    base_maker_pct: float = 0.0

    def summary(self) -> str:
        extra = []
        if self.referral_discount:
            extra.append(f"referral taker×{1.0 - self.referral_discount:g}")
        if self.maker_rebate_bps is not None:
            extra.append(f"ALO rebate {self.maker_rebate_bps:g} bp")
        extra.append(ALO_TIF_NOTE)
        return (
            f"fee tier {self.tier}  growthMode={'enabled' if self.growth_mode else 'off'}  "
            f"all-in taker {self.taker_pct:.4f}% / maker {self.maker_pct:.4f}%  "
            + "  ".join(extra)
        )


def hip3_fee_scale(deployer_fee_scale: float) -> float:
    if deployer_fee_scale < 1:
        return deployer_fee_scale + 1.0
    return deployer_fee_scale * 2.0


def rates_for_tier(tier: int) -> tuple[float, float]:
    if tier not in PERP_VOLUME_TIERS:
        known = ", ".join(str(k) for k in sorted(PERP_VOLUME_TIERS))
        raise ConfigError(f"FEE_TIER must be one of {known}, got {tier}")
    return PERP_VOLUME_TIERS[tier]


def rebate_pct_from_bps(bps: float) -> float:
    """Convert basis points of notional into percentage points (1 bp = 0.01%)."""
    return abs(float(bps)) * 0.01


def alo_fill_rebate_usd(notional_usd: float, maker_rebate_bps: float) -> float:
    return float(notional_usd) * (abs(float(maker_rebate_bps)) / 10_000.0)


def estimate_fees(
    *,
    deployer_fee_scale: float = 1.0,
    growth_mode: bool = True,
    taker_rate: float | None = None,
    maker_rate: float | None = None,
    referral_discount: float = 0.0,
    maker_rebate_bps: float | None = None,
    tier: int = DEFAULT_FEE_TIER,
) -> FeeEstimate:
    if not 0.0 <= referral_discount < 1.0:
        raise ConfigError("REFERRAL_DISCOUNT must be in [0, 1)")
    base_taker, base_maker = rates_for_tier(tier)
    if taker_rate is None:
        taker_rate = base_taker
    if maker_rate is None:
        maker_rate = base_maker
    scale_if_hip3 = hip3_fee_scale(deployer_fee_scale)
    growth_scale = 0.1 if growth_mode else 1.0
    # Referral applies to taker only. Do not invent a referral % when unset.
    taker_pct = taker_rate * 100.0 * scale_if_hip3 * growth_scale * (1.0 - referral_discount)
    maker_pct = maker_rate * 100.0 * growth_scale
    if maker_pct > 0:
        maker_pct *= scale_if_hip3
    if maker_rebate_bps is not None:
        maker_pct -= rebate_pct_from_bps(maker_rebate_bps)
    return FeeEstimate(
        taker_pct=taker_pct,
        maker_pct=maker_pct,
        scale_if_hip3=scale_if_hip3,
        growth_mode_scale=growth_scale,
        deployer_fee_scale=deployer_fee_scale,
        growth_mode=growth_mode,
        tier=tier,
        referral_discount=referral_discount,
        maker_rebate_bps=maker_rebate_bps,
        base_taker_pct=taker_rate * 100.0,
        base_maker_pct=maker_rate * 100.0,
    )


def estimate_market_fees(
    market: Market,
    *,
    fee_tier: int = DEFAULT_FEE_TIER,
    referral_discount: float = 0.0,
    maker_rebate_bps: float | None = None,
) -> FeeEstimate:
    return estimate_fees(
        deployer_fee_scale=market.deployer_fee_scale,
        growth_mode=market.growth_mode,
        referral_discount=referral_discount,
        maker_rebate_bps=maker_rebate_bps,
        tier=fee_tier,
    )


def estimate_from_settings(market: Market, settings: object) -> FeeEstimate:
    return estimate_market_fees(
        market,
        fee_tier=getattr(settings, "fee_tier", DEFAULT_FEE_TIER),
        referral_discount=getattr(settings, "referral_discount", 0.0),
        maker_rebate_bps=getattr(settings, "maker_rebate_bps", None),
    )


def fee_for_notional(estimate: FeeEstimate, notional_usd: float, *, maker: bool) -> float:
    pct = estimate.maker_pct if maker else estimate.taker_pct
    return notional_usd * (pct / 100.0)


def describe_fee_model(settings: object) -> str:
    tier = getattr(settings, "fee_tier", DEFAULT_FEE_TIER)
    referral = getattr(settings, "referral_discount", 0.0)
    rebate = getattr(settings, "maker_rebate_bps", None)
    rebate_txt = f"{rebate:g} bp" if rebate is not None else "unset (maker fee 0)"
    return (
        f"fee model: volume tier {tier} (14d)  growthMode from io meta  "
        f"REFERRAL_DISCOUNT={referral:g}  MAKER_REBATE_BPS={rebate_txt}  "
        f"{ALO_TIF_NOTE}"
    )
