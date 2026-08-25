"""ALO two-sided quotes from a multiplexed L2 book."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from entropy_bot.coins import Market
from entropy_bot.precision import round_price, size_from_notional, spread_bps, tick_size


@dataclass
class BookTop:
    coin: str
    bid: float | None
    ask: float | None
    bid_sz: float | None
    ask_sz: float | None
    mid: float | None
    time: int | None

    @property
    def spread_bps(self) -> float | None:
        if self.bid is None or self.ask is None:
            return None
        return spread_bps(self.bid, self.ask)


@dataclass
class QuoteIntent:
    coin: str
    asset_id: int
    is_buy: bool
    px: float
    sz: float
    side: str


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


def book_top(coin: str, data: dict[str, Any]) -> BookTop:
    levels = data.get("levels") or [[], []]
    bids = levels[0] if levels else []
    asks = levels[1] if len(levels) > 1 else []
    bid = float(bids[0]["px"]) if bids else None
    ask = float(asks[0]["px"]) if asks else None
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
    )


def desired_quotes(
    market: Market,
    top: BookTop,
    *,
    notional_usd: float,
    offset_ticks: int,
) -> list[QuoteIntent]:
    if top.bid is None or top.ask is None or top.mid is None:
        return []
    tick = tick_size(top.mid, market.sz_decimals)
    offset = max(0, offset_ticks) * tick
    raw_bid = top.bid - offset
    raw_ask = top.ask + offset
    bid_px = round_price(raw_bid, market.sz_decimals, side="bid")
    ask_px = round_price(raw_ask, market.sz_decimals, side="ask")
    # ALO must rest: bid strictly below best ask, ask strictly above best bid.
    if bid_px >= top.ask:
        bid_px = round_price(top.ask - tick, market.sz_decimals, side="bid")
    if ask_px <= top.bid:
        ask_px = round_price(top.bid + tick, market.sz_decimals, side="ask")
    if bid_px <= 0 or ask_px <= 0 or bid_px >= ask_px:
        return []
    bid_sz = size_from_notional(notional_usd, bid_px, market.sz_decimals)
    ask_sz = size_from_notional(notional_usd, ask_px, market.sz_decimals)
    if bid_sz <= 0 or ask_sz <= 0:
        return []
    return [
        QuoteIntent(market.coin, market.asset_id, True, bid_px, bid_sz, "B"),
        QuoteIntent(market.coin, market.asset_id, False, ask_px, ask_sz, "A"),
    ]


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
