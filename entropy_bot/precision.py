"""Hyperliquid tick / lot rounding (perps).

Prices: 5 significant figures, at most MAX_DECIMALS - szDecimals decimals
(MAX_DECIMALS=6 for perps). Integer prices are always allowed.
Sizes: rounded to szDecimals.
"""

from __future__ import annotations

from decimal import Decimal, ROUND_CEILING, ROUND_DOWN

MAX_DECIMALS_PERP = 6
SIG_FIGS = 5


def max_price_decimals(sz_decimals: int) -> int:
    return max(0, MAX_DECIMALS_PERP - int(sz_decimals))


def tick_size(price: float, sz_decimals: int) -> float:
    """Smallest valid increment near `price` (sig-fig or decimal cap)."""
    if price <= 0:
        return 10 ** -max_price_decimals(sz_decimals)
    dec_cap = Decimal(10) ** -max_price_decimals(sz_decimals)
    if price >= 100_000:
        return 1.0
    mag = Decimal(10) ** (Decimal(price).adjusted() - (SIG_FIGS - 1))
    tick = mag if mag > dec_cap else dec_cap
    return float(tick)


def round_price(price: float, sz_decimals: int, *, side: str = "nearest") -> float:
    if price <= 0:
        raise ValueError("price must be positive")
    if price > 100_000:
        return float(round(price))
    sig = float(f"{price:.5g}")
    px = round(sig, max_price_decimals(sz_decimals))
    if side == "bid":
        # never round a bid up through the touch
        tick = tick_size(px, sz_decimals)
        if px > price + tick * 1e-9:
            px = round_price(price - tick / 2, sz_decimals, side="nearest")
    elif side == "ask":
        tick = tick_size(px, sz_decimals)
        if px < price - tick * 1e-9:
            px = round_price(price + tick / 2, sz_decimals, side="nearest")
    return px


def floor_size(size: float, sz_decimals: int) -> float:
    quant = Decimal(10) ** -int(sz_decimals)
    floored = Decimal(str(size)).quantize(quant, rounding=ROUND_DOWN)
    return float(floored)


def wire_number(value: float) -> str:
    """Official wire format: 8 d.p. then strip trailing zeros."""
    rounded = f"{value:.8f}"
    normalized = Decimal(rounded).normalize()
    text = f"{normalized:f}"
    if text == "-0":
        return "0"
    return text


def size_from_notional(notional_usd: float, price: float, sz_decimals: int) -> float:
    if price <= 0:
        raise ValueError("price must be positive")
    return floor_size(notional_usd / price, sz_decimals)


def spread_bps(bid: float, ask: float) -> float | None:
    if bid <= 0 or ask <= 0 or ask < bid:
        return None
    mid = (bid + ask) / 2.0
    if mid <= 0:
        return None
    return (ask - bid) / mid * 10_000.0


def isolated_leverage(requested: int, market_max: int) -> int:
    cap = min(int(requested), int(market_max))
    return max(1, cap)


def tick_from_wire(px: object) -> float | None:
    """Infer a tick from a live book price string (trailing zeros ignored)."""
    if px is None:
        return None
    text = str(px).strip()
    if not text:
        return None
    if "." not in text:
        return 1.0
    frac = text.split(".", 1)[1].rstrip("0")
    return float(Decimal(10) ** -len(frac))


def floor_to_tick(price: float, tick: float) -> float:
    if tick <= 0:
        raise ValueError("tick must be positive")
    p = Decimal(str(price))
    t = Decimal(str(tick))
    steps = (p / t + Decimal("1e-8")).to_integral_value(rounding=ROUND_DOWN)
    return float(steps * t)


def ceil_to_tick(price: float, tick: float) -> float:
    if tick <= 0:
        raise ValueError("tick must be positive")
    p = Decimal(str(price))
    t = Decimal(str(tick))
    steps = (p / t - Decimal("1e-8")).to_integral_value(rounding=ROUND_CEILING)
    return float(steps * t)


def book_tick(
    bid: float | None,
    ask: float | None,
    sz_decimals: int,
    *,
    bid_px: str | None = None,
    ask_px: str | None = None,
    mid: float | None = None,
) -> float:
    """Live bid/ask increment, not tick_size(mid) (which is 1.0 near ANTH ~1985)."""
    ticks: list[float] = []
    for raw in (bid_px, ask_px):
        inferred = tick_from_wire(raw)
        if inferred is not None and inferred > 0:
            ticks.append(inferred)
    if bid is not None and bid > 0:
        ticks.append(tick_size(bid, sz_decimals))
    if ask is not None and ask > 0:
        ticks.append(tick_size(ask, sz_decimals))
    if ticks:
        return min(ticks)
    src = mid if mid and mid > 0 else bid if bid and bid > 0 else ask
    if src is None or src <= 0:
        return 10 ** -max_price_decimals(sz_decimals)
    return tick_size(src, sz_decimals)
