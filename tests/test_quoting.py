from __future__ import annotations

from entropy_bot.precision import book_tick, tick_from_wire
from entropy_bot.quoting import (
    FLAT_FAR_MS,
    FLAT_STALE_MS,
    PaperState,
    QuoteIntent,
    book_top,
    desired_quotes,
    detect_paper_fills,
    flatten_stage,
    install_quotes,
    quote_plan,
    sync_pos_since,
)


def _book(coin: str, bid: str, ask: str) -> dict:
    return {
        "levels": [
            [{"px": bid, "sz": "1", "n": 1}],
            [{"px": ask, "sz": "1", "n": 1}],
        ]
    }


def test_paper_fill_when_ask_crosses_bid(markets):
    state = PaperState()
    install_quotes(
        state,
        [
            QuoteIntent("io:ANTH", 200_001, True, 1979.0, 0.02, "B"),
            QuoteIntent("io:ANTH", 200_001, False, 1981.0, 0.02, "A"),
        ],
    )
    top = book_top("io:ANTH", _book("io:ANTH", "1981.2", "1981.5"))
    fills = detect_paper_fills(state, top)
    assert len(fills) == 1
    assert fills[0].side == "A"
    assert state.position["io:ANTH"] == -0.02


def test_desired_quotes_stay_post_only(markets):
    top = book_top("io:ANTH", _book("io:ANTH", "1979.4", "1979.5"))
    intents = desired_quotes(markets["io:ANTH"], top, notional_usd=50, offset_ticks=2)
    bid = next(i for i in intents if i.is_buy)
    ask = next(i for i in intents if not i.is_buy)
    assert bid.px < top.ask
    assert ask.px > top.bid
    assert bid.asset_id == 200_001
    # tight book (1 tick) joins the touch
    assert bid.px == 1979.4
    assert ask.px == 1979.5


def test_wide_book_improves_toward_mid(markets):
    """Userscript 1.6.1: 1985.00 / 1986.90 / tick 0.1 → 1985.8 / 1986.1."""
    top = book_top("io:ANTH", _book("io:ANTH", "1985.00", "1986.90"))
    assert top.bid == 1985.0
    assert top.ask == 1986.9
    # Wire of an integer-looking mid looks like a $1 tick; live ask increment is $0.1.
    from entropy_bot.precision import round_price, wire_number

    assert tick_from_wire(wire_number(round_price(1985, 3))) == 1.0
    tick = top.inferred_tick(markets["io:ANTH"].sz_decimals)
    assert abs(tick - 0.1) < 1e-12
    assert abs(book_tick(top.bid, top.ask, 3, bid_px=top.bid_px, ask_px=top.ask_px, mid=top.mid) - 0.1) < 1e-12
    intents = desired_quotes(markets["io:ANTH"], top, notional_usd=50)
    bid = next(i for i in intents if i.is_buy)
    ask = next(i for i in intents if not i.is_buy)
    assert bid.px == 1985.8
    assert ask.px == 1986.1
    assert bid.px < ask.px
    assert bid.reduce_only is False
    assert ask.tif == "Alo"


def test_flatten_stages(markets):
    top = book_top("io:ANTH", _book("io:ANTH", "1985.00", "1986.90"))
    market = markets["io:ANTH"]
    assert flatten_stage(0, 0.0) == "flat"
    assert flatten_stage(1000, 0.02) == "far"
    assert flatten_stage(FLAT_FAR_MS, 0.02) == "mid"
    assert flatten_stage(16_000, 0.02) == "mid"
    assert flatten_stage(FLAT_STALE_MS, 0.02) == "mid"

    far = quote_plan(market, top, szi=0.02, age_ms=1000, notional_usd=50)
    assert far.mode == "long" and far.stage == "far" and far.tif == "Alo" and not far.take
    assert len(far.intents) == 1
    sell = far.intents[0]
    assert sell.is_buy is False and sell.reduce_only is True
    assert sell.px == 1986.9  # far touch ask
    assert sell.sz == min(0.02, round(50 / 1986.9, 3)) or sell.sz <= 0.02

    mid = quote_plan(market, top, szi=0.02, age_ms=7_000, notional_usd=50)
    assert mid.stage == "mid" and mid.tif == "Alo" and not mid.take
    assert mid.intents[0].is_buy is False and mid.intents[0].reduce_only is True
    # mid or 1 tick inside near touch (bid+tick)
    assert 1985.0 < mid.intents[0].px <= 1986.9
    assert mid.intents[0].px != 1985.0  # never sell the bid (that was IOC take)

    aged = quote_plan(market, top, szi=0.02, age_ms=16_000, notional_usd=50)
    assert aged.stage == "mid" and not aged.take and aged.tif == "Alo"
    assert aged.intents[0].reduce_only is True
    assert aged.intents[0].tif == "Alo"
    assert aged.intents[0].px == mid.intents[0].px  # still maker mid, not bid take
    assert aged.intents[0].px != 1985.0

    short = quote_plan(market, top, szi=-0.02, age_ms=1000, notional_usd=50)
    assert short.mode == "short" and short.intents[0].is_buy is True
    assert short.intents[0].reduce_only is True
    assert short.intents[0].px == 1985.0  # far touch bid

    short_aged = quote_plan(market, top, szi=-0.02, age_ms=16_000, notional_usd=50)
    assert not short_aged.take and short_aged.tif == "Alo"
    assert short_aged.intents[0].tif == "Alo"
    assert short_aged.intents[0].px != 1986.9  # never cover the ask (that was IOC take)
    assert 1985.0 <= short_aged.intents[0].px < 1986.9

    flat = quote_plan(market, top, szi=0.0, age_ms=99_000, notional_usd=50)
    assert flat.mode == "flat" and not flat.take and len(flat.intents) == 2
    assert all(not i.reduce_only and i.tif == "Alo" for i in flat.intents)


def test_flatten_never_emits_ioc_any_age_any_coin(markets):
    ages = (0, 1_000, 6_000, 15_000, 16_000, 90_000, 180_000)
    sizes = (0.02, -0.02, 0.041, -0.041)
    for coin in ("io:ANTH", "io:SNDK"):
        top = book_top(coin, _book(coin, "1985.00", "1986.90"))
        for age in ages:
            for szi in sizes:
                plan = quote_plan(markets[coin], top, szi=szi, age_ms=age, notional_usd=50)
                assert plan.tif == "Alo"
                assert not plan.take
                assert plan.stage in {"flat", "far", "mid"}
                assert all(i.tif == "Alo" for i in plan.intents)
                assert all(i.reduce_only for i in plan.intents)
                assert len(plan.intents) == 1


def test_flatten_size_is_min_notional_and_position(markets):
    top = book_top("io:ANTH", _book("io:ANTH", "1985.00", "1986.90"))
    huge = quote_plan(markets["io:ANTH"], top, szi=10.0, age_ms=1000, notional_usd=50)
    notional_sz = 50 / 1986.9
    assert huge.intents[0].sz <= notional_sz + 1e-9
    tiny = quote_plan(markets["io:ANTH"], top, szi=0.001, age_ms=1000, notional_usd=50)
    assert tiny.intents[0].sz == 0.001


def test_pos_age_resets_on_flat_and_flip():
    clocks = sync_pos_since({}, {"io:ANTH": 0.02}, {}, 100.0)
    assert clocks["io:ANTH"] == 100.0
    clocks = sync_pos_since({"io:ANTH": 0.02}, {"io:ANTH": 0.02}, clocks, 110.0)
    assert clocks["io:ANTH"] == 100.0
    clocks = sync_pos_since({"io:ANTH": 0.02}, {"io:ANTH": 0.0}, clocks, 120.0)
    assert clocks["io:ANTH"] == 0.0
    clocks = sync_pos_since({"io:ANTH": 0.0}, {"io:ANTH": -0.01}, clocks, 130.0)
    assert clocks["io:ANTH"] == 130.0
    clocks = sync_pos_since({"io:ANTH": -0.01}, {"io:ANTH": 0.01}, clocks, 140.0)
    assert clocks["io:ANTH"] == 140.0
