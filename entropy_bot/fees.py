"""HIP-3 fee estimate: HL volume tier 4 + Entropy Partner self-rebate.

Hyperliquid volume tier 4 (14d > $500M): taker 0.028% / maker 0.000%.
HIP-3 on EntropyIO: deployerFeeScale=1 → scaleIfHip3=2, growthMode ×0.1
→ **gross** taker 0.0056% / maker 0%.

Entropy Partner (Tier 4) self-rebate is 200% of **Entropy's deployer share**,
not the full user fee. With deployerFeeScale>=1 the user fee splits 50/50
Hyperliquid / Entropy. See https://docs.entropy.io/about-entropy/referrals.md

    gross_taker = 0.028% * scaleIfHip3 * growthScale   # 0.0056%
    entropy_share = 0.5 if deployerFeeScale>=1 else scale/(1+scale)
    self_rebate = gross * entropy_share * ENTROPY_SELF_REBATE  # default 2.0
    net_taker = gross_taker - self_rebate                      # 0

优惠 100% is Referral reward 100% + Referred-user benefit 100% for invitees.
It is NOT 100% off the full Hyperliquid fee, and is NOT applied to this
bot's own fills unless ENTROPY_REFERRED_USER_BENEFIT is set.
"""

from __future__ import annotations

from dataclasses import dataclass

from entropy_bot.coins import Market
from entropy_bot.errors import ConfigError

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
DEFAULT_ENTROPY_TIER = 4
DEFAULT_ENTROPY_SELF_REBATE = 2.0
DEFAULT_ENTROPY_REFERRAL_REWARD = 1.0
DEFAULT_ENTROPY_REFERRED_USER_BENEFIT = 0.0

# Official Entropy early-bird program (percent of Entropy's share only).
# Referred-user benefit from this table is display/invitee-only.
ENTROPY_PROGRAM_TIERS: dict[int, dict[str, float | str]] = {
    1: {"name": "Trader", "self_rebate": 0.6, "referral_reward": 0.25, "referred_benefit": 0.25},
    2: {"name": "OG", "self_rebate": 1.2, "referral_reward": 0.5, "referred_benefit": 0.5},
    3: {"name": "Preferred", "self_rebate": 1.6, "referral_reward": 0.75, "referred_benefit": 0.75},
    4: {"name": "Partner", "self_rebate": 2.0, "referral_reward": 1.0, "referred_benefit": 1.0},
}

MAKER_SHARE_REBATE_BPS: dict[float, float] = {
    0.5: 0.1,
    1.5: 0.2,
    3.0: 0.3,
}

ALO_TIF_NOTE = "default TIF=ALO (two-sided maker quotes); flatten/exit is maker ALO only (no IOC)"


@dataclass(frozen=True)
class FeeSplitExample:
    total_fee: float
    hyperliquid: float
    entropy: float
    self_rebate: float
    referred_user_benefit: float
    net: float


@dataclass(frozen=True)
class FeeEstimate:
    taker_pct: float  # net taker after Entropy self-rebate
    maker_pct: float  # net maker
    gross_taker_pct: float
    gross_maker_pct: float
    self_rebate_taker_pct: float
    self_rebate_maker_pct: float
    referred_benefit_taker_pct: float
    entropy_share: float
    scale_if_hip3: float
    growth_mode_scale: float
    deployer_fee_scale: float
    growth_mode: bool
    tier: int
    entropy_tier: int
    entropy_tier_name: str
    entropy_self_rebate: float
    entropy_referral_reward: float
    entropy_referred_user_benefit: float
    referral_discount: float = 0.0
    maker_rebate_bps: float | None = None
    base_taker_pct: float = 0.028
    base_maker_pct: float = 0.0

    def summary(self) -> str:
        extra = []
        if self.referral_discount:
            extra.append(f"HL REFERRAL_DISCOUNT taker×{1.0 - self.referral_discount:g}")
        if self.maker_rebate_bps is not None:
            extra.append(f"ALO rebate {self.maker_rebate_bps:g} bp")
        if self.entropy_referred_user_benefit:
            extra.append(f"referred-user benefit {self.entropy_referred_user_benefit:.0%} of deployer share")
        else:
            extra.append("优惠/referred-user benefit not applied to own fills")
        extra.append(ALO_TIF_NOTE)
        return (
            f"Entropy {self.entropy_tier_name} T{self.entropy_tier}  "
            f"self rebate {self.entropy_self_rebate:.0%} of deployer share "
            f"({self.entropy_share:.0%} Entropy / {1.0 - self.entropy_share:.0%} HL)  "
            f"HL volume tier {self.tier}  growthMode={'enabled' if self.growth_mode else 'off'}  "
            f"gross taker {self.gross_taker_pct:.4f}% → net {self.taker_pct:.4f}%  "
            f"gross maker {self.gross_maker_pct:.4f}% → net {self.maker_pct:.4f}%  "
            + "  ".join(extra)
        )


def hip3_fee_scale(deployer_fee_scale: float) -> float:
    if deployer_fee_scale < 1:
        return deployer_fee_scale + 1.0
    return deployer_fee_scale * 2.0


def entropy_deployer_share(deployer_fee_scale: float) -> float:
    """Entropy's fraction of the *user* HIP-3 fee (not 100% of the fee)."""
    if deployer_fee_scale >= 1:
        return 0.5
    return deployer_fee_scale / (1.0 + deployer_fee_scale)


def entropy_program(tier: int) -> dict[str, float | str]:
    if tier not in ENTROPY_PROGRAM_TIERS:
        known = ", ".join(str(k) for k in sorted(ENTROPY_PROGRAM_TIERS))
        raise ConfigError(f"ENTROPY_TIER must be one of {known}, got {tier}")
    return ENTROPY_PROGRAM_TIERS[tier]


def rates_for_tier(tier: int) -> tuple[float, float]:
    if tier not in PERP_VOLUME_TIERS:
        known = ", ".join(str(k) for k in sorted(PERP_VOLUME_TIERS))
        raise ConfigError(f"FEE_TIER must be one of {known}, got {tier}")
    return PERP_VOLUME_TIERS[tier]


def rebate_pct_from_bps(bps: float) -> float:
    return abs(float(bps)) * 0.01


def alo_fill_rebate_usd(notional_usd: float, maker_rebate_bps: float) -> float:
    return float(notional_usd) * (abs(float(maker_rebate_bps)) / 10_000.0)


def _share_rebate(gross_pct: float, share: float, rate: float) -> float:
    if gross_pct <= 0 or rate <= 0:
        return 0.0
    return gross_pct * share * rate


def worked_fee_example(
    total_fee: float = 100.0,
    *,
    deployer_fee_scale: float = 1.0,
    self_rebate: float = DEFAULT_ENTROPY_SELF_REBATE,
    referred_user_benefit: float = 0.0,
) -> FeeSplitExample:
    """Docs $100 example: percentages apply only to Entropy's share."""
    share = entropy_deployer_share(deployer_fee_scale)
    entropy = total_fee * share
    hyperliquid = total_fee - entropy
    rebate = entropy * self_rebate
    referred = entropy * referred_user_benefit
    return FeeSplitExample(
        total_fee=total_fee,
        hyperliquid=hyperliquid,
        entropy=entropy,
        self_rebate=rebate,
        referred_user_benefit=referred,
        net=total_fee - rebate - referred,
    )


def estimate_fees(
    *,
    deployer_fee_scale: float = 1.0,
    growth_mode: bool = True,
    taker_rate: float | None = None,
    maker_rate: float | None = None,
    referral_discount: float = 0.0,
    maker_rebate_bps: float | None = None,
    tier: int = DEFAULT_FEE_TIER,
    entropy_tier: int = DEFAULT_ENTROPY_TIER,
    entropy_self_rebate: float = DEFAULT_ENTROPY_SELF_REBATE,
    entropy_referral_reward: float = DEFAULT_ENTROPY_REFERRAL_REWARD,
    entropy_referred_user_benefit: float = DEFAULT_ENTROPY_REFERRED_USER_BENEFIT,
) -> FeeEstimate:
    if not 0.0 <= referral_discount < 1.0:
        raise ConfigError("REFERRAL_DISCOUNT must be in [0, 1)")
    if entropy_self_rebate < 0 or entropy_referred_user_benefit < 0 or entropy_referral_reward < 0:
        raise ConfigError("Entropy rebate rates must be >= 0")
    program = entropy_program(entropy_tier)
    base_taker, base_maker = rates_for_tier(tier)
    if taker_rate is None:
        taker_rate = base_taker
    if maker_rate is None:
        maker_rate = base_maker
    scale_if_hip3 = hip3_fee_scale(deployer_fee_scale)
    growth_scale = 0.1 if growth_mode else 1.0
    share = entropy_deployer_share(deployer_fee_scale)
    # Optional HL-side discount on gross only. Entropy 优惠 is not this knob.
    gross_taker = taker_rate * 100.0 * scale_if_hip3 * growth_scale * (1.0 - referral_discount)
    gross_maker = maker_rate * 100.0 * growth_scale
    if gross_maker > 0:
        gross_maker *= scale_if_hip3
    if maker_rebate_bps is not None:
        gross_maker -= rebate_pct_from_bps(maker_rebate_bps)
    self_taker = _share_rebate(gross_taker, share, entropy_self_rebate)
    referred_taker = _share_rebate(gross_taker, share, entropy_referred_user_benefit)
    self_maker = _share_rebate(gross_maker, share, entropy_self_rebate)
    referred_maker = _share_rebate(gross_maker, share, entropy_referred_user_benefit)
    return FeeEstimate(
        taker_pct=gross_taker - self_taker - referred_taker,
        maker_pct=gross_maker - self_maker - referred_maker,
        gross_taker_pct=gross_taker,
        gross_maker_pct=gross_maker,
        self_rebate_taker_pct=self_taker,
        self_rebate_maker_pct=self_maker,
        referred_benefit_taker_pct=referred_taker,
        entropy_share=share,
        scale_if_hip3=scale_if_hip3,
        growth_mode_scale=growth_scale,
        deployer_fee_scale=deployer_fee_scale,
        growth_mode=growth_mode,
        tier=tier,
        entropy_tier=entropy_tier,
        entropy_tier_name=str(program["name"]),
        entropy_self_rebate=entropy_self_rebate,
        entropy_referral_reward=entropy_referral_reward,
        entropy_referred_user_benefit=entropy_referred_user_benefit,
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
    entropy_tier: int = DEFAULT_ENTROPY_TIER,
    entropy_self_rebate: float = DEFAULT_ENTROPY_SELF_REBATE,
    entropy_referral_reward: float = DEFAULT_ENTROPY_REFERRAL_REWARD,
    entropy_referred_user_benefit: float = DEFAULT_ENTROPY_REFERRED_USER_BENEFIT,
) -> FeeEstimate:
    return estimate_fees(
        deployer_fee_scale=market.deployer_fee_scale,
        growth_mode=market.growth_mode,
        referral_discount=referral_discount,
        maker_rebate_bps=maker_rebate_bps,
        tier=fee_tier,
        entropy_tier=entropy_tier,
        entropy_self_rebate=entropy_self_rebate,
        entropy_referral_reward=entropy_referral_reward,
        entropy_referred_user_benefit=entropy_referred_user_benefit,
    )


def estimate_from_settings(market: Market, settings: object) -> FeeEstimate:
    return estimate_market_fees(
        market,
        fee_tier=getattr(settings, "fee_tier", DEFAULT_FEE_TIER),
        referral_discount=getattr(settings, "referral_discount", 0.0),
        maker_rebate_bps=getattr(settings, "maker_rebate_bps", None),
        entropy_tier=getattr(settings, "entropy_tier", DEFAULT_ENTROPY_TIER),
        entropy_self_rebate=getattr(settings, "entropy_self_rebate", DEFAULT_ENTROPY_SELF_REBATE),
        entropy_referral_reward=getattr(settings, "entropy_referral_reward", DEFAULT_ENTROPY_REFERRAL_REWARD),
        entropy_referred_user_benefit=getattr(
            settings, "entropy_referred_user_benefit", DEFAULT_ENTROPY_REFERRED_USER_BENEFIT
        ),
    )


def fee_for_notional(estimate: FeeEstimate, notional_usd: float, *, maker: bool) -> float:
    pct = estimate.maker_pct if maker else estimate.taker_pct
    return notional_usd * (pct / 100.0)


def describe_fee_model(settings: object) -> str:
    hl_tier = getattr(settings, "fee_tier", DEFAULT_FEE_TIER)
    e_tier = getattr(settings, "entropy_tier", DEFAULT_ENTROPY_TIER)
    program = entropy_program(e_tier)
    self_rebate = getattr(settings, "entropy_self_rebate", DEFAULT_ENTROPY_SELF_REBATE)
    reward = getattr(settings, "entropy_referral_reward", DEFAULT_ENTROPY_REFERRAL_REWARD)
    referred = getattr(settings, "entropy_referred_user_benefit", DEFAULT_ENTROPY_REFERRED_USER_BENEFIT)
    return (
        f"Entropy {program['name']} T{e_tier}  "
        f"self rebate {self_rebate:.0%} of deployer share (not the full HL fee)  "
        f"referral reward {reward:.0%} display-only  "
        f"referred-user benefit {referred:.0%} on own fills  "
        f"HL volume tier {hl_tier}  {ALO_TIF_NOTE}"
    )
