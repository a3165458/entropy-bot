"""ALO two-sided quotes and flatten ladder from a multiplexed L2 book.

Live MM (userscript 1.6.1):
- Tick comes from the live bid/ask increment, not tick_size(mid).
- Flat: join BBO when spread ≤ 2 ticks; otherwise improve toward mid.
- In position: reduce-only only — far ALO → mid ALO → IOC take.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from entropy_bot.coins import Market
from entropy_bot.precision import (
    book_tick,
    ceil_to_tick,
    floor_size,
    floor_to_tick,
    round_price,
    size_from_notional,
    spread_bps,
    tick_size,
)

FLAT_FAR_MS = 6_000
FLAT_TAKE_MS = 15_000
STALE_QUOTE_MS = 45_000

Stage = Literal["flat", "far", "mid", "take"]
Mode = Literal["flat", "long", "short"]


@dataclass
class BookTop:
    coin: str
    bid: float | None
    ask: float | None
    bid_sz: float | None
    ask_sz: float | None
    mid: float | None
    time: int | None
    bid_px: str | None = None
    ask_px: str | None = None

    @property
    def spread_bps(self) -> float | None:
        if self.bid is None or self.ask is None:
            return None
        return spread_bps(self.bid, self.ask)

    def inferred_tick(self, sz_decimals: int) -> float:
        return book_tick(
            self.bid,
            self.ask,
            sz_decimals,
            bid_px=self.bid_px,
            ask_px=self.ask_px,
            mid=self.mid,
        )


@dataclass
class QuoteIntent:
    coin: str
    asset_id: int
    is_buy: bool
    px: float
    sz: float
    side: str
    tif: str = "Alo"
    reduce_only: bool = False
    stage: str = "flat"


@dataclass
class QuotePlan:
    coin: str
    mode: Mode
    stage: Stage
    tif: str
    take: bool
    intents: list[QuoteIntent]
    age_ms: int
    szi: float
    buy_px: float | None = None
    sell_px: float | None = None

    def key(self) -> str:
        parts = [
            f"{self.buy_px}",
            f"{self.sell_px}",
            self.mode,
            self.stage,
            self.tif,
        ]
        for intent in self.intents:
            parts.append(
                f"{intent.side}:{intent.px}:{intent.sz}:{int(intent.reduce_only)}:{intent.tif}"
            )
        return "|".join(parts)


@dataclass
class PaperFill:
    coin: str
    side: str
    px: float
    sz: float
    reason: str


@dataclass
class PaperState:
    quotes: dict[str, dict[str, QuoteIntent]] = field(default_factory=dict)
    fills: list[PaperFill] = field(default_factory=list)
    position: dict[str, float] = field(default_factory=dict)


def _level_px(row: Any) -> tuple[float | None, str | None]:
    if not row:
        return None, None
    raw = row.get("px") if isinstance(row, dict) else None
    if raw is None:
        return None, None
    try:
        return float(raw), str(raw)
    except (TypeError, ValueError):
        return None, None


def book_top(coin: str, data: dict[str, Any]) -> BookTop:
    levels = data.get("levels") or [[], []]
    bids = levels[0] if levels else []
    asks = levels[1] if len(levels) > 1 else []
    bid, bid_px = _level_px(bids[0] if bids else None)
    ask, ask_px = _level_px(asks[0] if asks else None)
    bid_sz = float(bids[0]["sz"]) if bids else None
    ask_sz = float(asks[0]["sz"]) if asks else None
    mid = (bid + ask) / 2.0 if bid and ask else bid or ask
    return BookTop(
        coin=coin,
        bid=bid,
        ask=ask,
        bid_sz=bid_sz,
        ask_sz=ask_sz,
        mid=mid,
        time=data.get("time"),
        bid_px=bid_px,
        ask_px=ask_px,
    )


def _round_side(price: float, sz_decimals: int, *, side: str) -> float:
    return round_price(price, sz_decimals, side=side)


def alo_two_sided(market: Market, top: BookTop) -> tuple[float, float] | None:
    """Userscript 1.6.1 `aloPrices` + clamp: join tight books, improve wide ones."""
    if top.bid is None or top.ask is None or top.bid <= 0 or top.ask <= 0 or top.bid >= top.ask:
        return None
    tick = top.inferred_tick(market.sz_decimals)
    if tick <= 0:
        tick = tick_size(top.mid or top.bid, market.sz_decimals)
    spread = top.ask - top.bid
    if spread <= 2 * tick + 1e-9:
        buy = top.bid
        sell = top.ask
    else:
        mid = top.mid if top.mid is not None else (top.bid + top.ask) / 2.0
        buy = floor_to_tick(mid - tick, tick)
        sell = ceil_to_tick(mid + tick, tick)
        if buy <= 0 or sell <= 0 or buy >= sell:
            buy, sell = top.bid, top.ask
        else:
            cap = top.ask - tick
            floor = top.bid + tick
            if buy > cap:
                buy = cap
            if sell < floor:
                sell = floor
            if buy < top.bid:
                buy = top.bid
            if sell > top.ask:
                sell = top.ask
            if buy <= 0 or buy >= sell:
                buy, sell = top.bid, top.ask
    buy = _round_side(buy, market.sz_decimals, side="bid")
    sell = _round_side(sell, market.sz_decimals, side="ask")
    if buy >= sell:
        return None
    if buy >= top.ask:
        buy = _round_side(top.ask - tick, market.sz_decimals, side="bid")
    if sell <= top.bid:
        sell = _round_side(top.bid + tick, market.sz_decimals, side="ask")
    if buy <= 0 or sell <= 0 or buy >= sell:
        return None
    return buy, sell


def clamp_maker_buy(market: Market, top: BookTop, buy: float) -> float | None:
    if top.bid is None or top.ask is None or top.bid >= top.ask:
        return None
    tick = top.inferred_tick(market.sz_decimals)
    buy = min(buy, _round_side(top.ask - tick, market.sz_decimals, side="bid"))
    if buy < top.bid:
        buy = top.bid
    if buy <= 0 or buy >= top.ask:
        return None
    return buy


def clamp_maker_sell(market: Market, top: BookTop, sell: float) -> float | None:
    if top.bid is None or top.ask is None or top.bid >= top.ask:
        return None
    tick = top.inferred_tick(market.sz_decimals)
    sell = max(sell, _round_side(top.bid + tick, market.sz_decimals, side="ask"))
    if top.ask and sell > top.ask:
        sell = top.ask
    if sell <= 0 or sell <= top.bid:
        return None
    return sell


def clamp_maker_px(market: Market, top: BookTop, buy: float, sell: float) -> tuple[float, float] | None:
    buy_c = clamp_maker_buy(market, top, buy)
    sell_c = clamp_maker_sell(market, top, sell)
    if buy_c is None or sell_c is None or buy_c >= sell_c:
        return None
    return buy_c, sell_c


def desired_quotes(
    market: Market,
    top: BookTop,
    *,
    notional_usd: float,
    offset_ticks: int = 0,
) -> list[QuoteIntent]:
    """Two-sided ALO for a flat book. `offset_ticks` is unused (legacy paper knob)."""
    del offset_ticks
    px = alo_two_sided(market, top)
    if px is None:
        return []
    buy_px, sell_px = px
    bid_sz = size_from_notional(notional_usd, buy_px, market.sz_decimals)
    ask_sz = size_from_notional(notional_usd, sell_px, market.sz_decimals)
    if bid_sz <= 0 or ask_sz <= 0:
        return []
    return [
        QuoteIntent(market.coin, market.asset_id, True, buy_px, bid_sz, "B"),
        QuoteIntent(market.coin, market.asset_id, False, sell_px, ask_sz, "A"),
    ]


def flatten_stage(age_ms: int, szi: float) -> Stage:
    if abs(szi) <= 0:
        return "flat"
    if age_ms >= FLAT_TAKE_MS:
        return "take"
    if age_ms >= FLAT_FAR_MS:
        return "mid"
    return "far"


def flatten_size(abs_szi: float, notional_sz: float, sz_decimals: int) -> float:
    use = min(abs(abs_szi), notional_sz) if notional_sz > 0 else abs(abs_szi)
    return floor_size(use, sz_decimals)


def flatten_prices(market: Market, top: BookTop, stage: Stage) -> tuple[float, float] | None:
    if top.bid is None or top.ask is None or top.bid <= 0 or top.ask <= 0:
        return None
    tick = top.inferred_tick(market.sz_decimals)
    if stage == "take":
        return top.ask, top.bid  # short cover @ask / long exit @bid
    if stage == "mid":
        mid = top.mid if top.mid and top.mid > 0 else (top.bid + top.ask) / 2.0
        mid_px = _round_side(mid, market.sz_decimals, side="nearest")
        inside_buy = _round_side(top.ask - tick, market.sz_decimals, side="bid")
        inside_sell = _round_side(top.bid + tick, market.sz_decimals, side="ask")
        buy = mid_px if mid_px > 0 else top.bid
        sell = mid_px if mid_px > 0 else top.ask
        if inside_buy > 0 and buy > inside_buy:
            buy = inside_buy
        if inside_sell > 0 and sell < inside_sell:
            sell = inside_sell
        return buy, sell
    # far touch: long sells the ask, short buys the bid
    return top.bid, top.ask


def quote_plan(
    market: Market,
    top: BookTop,
    *,
    szi: float,
    age_ms: int,
    notional_usd: float,
) -> QuotePlan:
    stage = flatten_stage(age_ms, szi)
    if abs(szi) <= 0:
        px = alo_two_sided(market, top)
        intents: list[QuoteIntent] = []
        buy_px = sell_px = None
        if px is not None:
            buy_px, sell_px = px
            buy_sz = size_from_notional(notional_usd, buy_px, market.sz_decimals)
            sell_sz = size_from_notional(notional_usd, sell_px, market.sz_decimals)
            if buy_sz > 0:
                intents.append(
                    QuoteIntent(market.coin, market.asset_id, True, buy_px, buy_sz, "B", "Alo", False, "flat")
                )
            if sell_sz > 0:
                intents.append(
                    QuoteIntent(market.coin, market.asset_id, False, sell_px, sell_sz, "A", "Alo", False, "flat")
                )
        return QuotePlan(market.coin, "flat", "flat", "Alo", False, intents, 0, 0.0, buy_px, sell_px)

    take = stage == "take"
    tif = "Ioc" if take else "Alo"
    raw = flatten_prices(market, top, stage)
    if raw is None:
        return QuotePlan(market.coin, "long" if szi > 0 else "short", stage, tif, take, [], age_ms, szi)
    buy_raw, sell_raw = raw
    intents: list[QuoteIntent] = []
    buy_px = sell_px = None
    if szi > 0:
        if not take:
            sell_raw_c = clamp_maker_sell(market, top, sell_raw)
            if sell_raw_c is None:
                return QuotePlan(market.coin, "long", stage, tif, take, [], age_ms, szi)
            sell_raw = sell_raw_c
        notional_sz = size_from_notional(notional_usd, sell_raw, market.sz_decimals)
        sz = flatten_size(abs(szi), notional_sz, market.sz_decimals)
        if sz > 0:
            sell_px = sell_raw
            intents.append(
                QuoteIntent(market.coin, market.asset_id, False, sell_raw, sz, "A", tif, True, stage)
            )
        return QuotePlan(market.coin, "long", stage, tif, take, intents, age_ms, szi, None, sell_px)

    if not take:
        buy_raw_c = clamp_maker_buy(market, top, buy_raw)
        if buy_raw_c is None:
            return QuotePlan(market.coin, "short", stage, tif, take, [], age_ms, szi)
        buy_raw = buy_raw_c
    notional_sz = size_from_notional(notional_usd, buy_raw, market.sz_decimals)
    sz = flatten_size(abs(szi), notional_sz, market.sz_decimals)
    if sz > 0:
        buy_px = buy_raw
        intents.append(
            QuoteIntent(market.coin, market.asset_id, True, buy_raw, sz, "B", tif, True, stage)
        )
    return QuotePlan(market.coin, "short", stage, tif, take, intents, age_ms, szi, buy_px, None)


def sync_pos_since(
    previous: dict[str, float],
    incoming: dict[str, float],
    clocks: dict[str, float],
    now: float,
) -> dict[str, float]:
    """Age starts when |szi| first becomes nonzero; resets on flat or flip."""
    updated = dict(clocks)
    for coin, szi in incoming.items():
        had = previous.get(coin, 0.0)
        if abs(szi) <= 0:
            updated[coin] = 0.0
        elif abs(had) <= 0 or (had > 0) != (szi > 0) or not updated.get(coin):
            updated[coin] = now
    return updated


def detect_paper_fills(
    state: PaperState,
    top: BookTop,
) -> list[PaperFill]:
    fills: list[PaperFill] = []
    live = state.quotes.get(top.coin) or {}
    bid = live.get("B")
    ask = live.get("A")
    if bid and top.ask is not None and top.ask <= bid.px:
        sz = min(bid.sz, top.ask_sz or bid.sz)
        if sz > 0:
            fills.append(PaperFill(top.coin, "B", bid.px, sz, "ask crossed paper bid"))
    if ask and top.bid is not None and top.bid >= ask.px:
        sz = min(ask.sz, top.bid_sz or ask.sz)
        if sz > 0:
            fills.append(PaperFill(top.coin, "A", ask.px, sz, "bid crossed paper ask"))
    for fill in fills:
        signed = fill.sz if fill.side == "B" else -fill.sz
        state.position[fill.coin] = state.position.get(fill.coin, 0.0) + signed
        state.fills.append(fill)
    return fills


def install_quotes(state: PaperState, intents: list[QuoteIntent]) -> None:
    by_coin: dict[str, dict[str, QuoteIntent]] = {}
    for intent in intents:
        by_coin.setdefault(intent.coin, {})[intent.side] = intent
    for coin, sides in by_coin.items():
        state.quotes[coin] = sides
