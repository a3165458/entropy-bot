"""Growth-mode HIP-3 fee estimate (official Hyperliquid fee math).

At tier 0, base rates are taker 0.045% / maker 0.015%. For HIP-3:

    scaleIfHip3 = deployerFeeScale + 1   if deployerFeeScale < 1
                = deployerFeeScale * 2   otherwise
    growthModeScale = 0.1 if growthMode else 1

With deployerFeeScale=1 and growthMode enabled:
    scaleIfHip3=2, growth=0.1 → taker 0.009% / maker 0.003%.
"""

from __future__ import annotations

from dataclasses import dataclass

from entropy_bot.coins import Market

TIER0_TAKER_RATE = 0.00045  # 0.045%
TIER0_MAKER_RATE = 0.00015  # 0.015%


@dataclass(frozen=True)
class FeeEstimate:
    taker_pct: float
    maker_pct: float
    scale_if_hip3: float
    growth_mode_scale: float
    deployer_fee_scale: float
    growth_mode: bool
    tier: int

    def summary(self) -> str:
        return (
            f"est fees (tier {self.tier}, HIP-3 scale={self.scale_if_hip3:g}, "
            f"growth×{self.growth_mode_scale:g}): "
            f"taker {self.taker_pct:.4f}% / maker {self.maker_pct:.4f}%"
        )


def hip3_fee_scale(deployer_fee_scale: float) -> float:
    if deployer_fee_scale < 1:
        return deployer_fee_scale + 1.0
    return deployer_fee_scale * 2.0


def estimate_fees(
    *,
    deployer_fee_scale: float = 1.0,
    growth_mode: bool = True,
    taker_rate: float = TIER0_TAKER_RATE,
    maker_rate: float = TIER0_MAKER_RATE,
    referral_discount: float = 0.0,
    tier: int = 0,
) -> FeeEstimate:
    scale_if_hip3 = hip3_fee_scale(deployer_fee_scale)
    growth_scale = 0.1 if growth_mode else 1.0
    discount = 1.0 - referral_discount
    taker_pct = taker_rate * 100.0 * scale_if_hip3 * growth_scale * discount
    maker_pct = maker_rate * 100.0 * growth_scale
    if maker_pct > 0:
        maker_pct *= scale_if_hip3 * discount
    return FeeEstimate(
        taker_pct=taker_pct,
        maker_pct=maker_pct,
        scale_if_hip3=scale_if_hip3,
        growth_mode_scale=growth_scale,
        deployer_fee_scale=deployer_fee_scale,
        growth_mode=growth_mode,
        tier=tier,
    )


def estimate_market_fees(market: Market, *, referral_discount: float = 0.0) -> FeeEstimate:
    return estimate_fees(
        deployer_fee_scale=market.deployer_fee_scale,
        growth_mode=market.growth_mode,
        referral_discount=referral_discount,
    )


def fee_for_notional(estimate: FeeEstimate, notional_usd: float, *, maker: bool) -> float:
    pct = estimate.maker_pct if maker else estimate.taker_pct
    return notional_usd * (pct / 100.0)
