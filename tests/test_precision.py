from __future__ import annotations

from entropy_bot.precision import (
    book_tick,
    ceil_to_tick,
    floor_to_tick,
    round_price,
    tick_from_wire,
    wire_number,
)


def test_tick_from_wire_strips_trailing_zeros():
    assert tick_from_wire("1985.00") == 1.0
    assert tick_from_wire("1986.90") == 0.1
    assert tick_from_wire("1986.9") == 0.1
    assert tick_from_wire("1504") == 1.0


def test_book_tick_prefers_live_increment_not_sigfig_tick():
    # Userscript tickSize(roundPx(1985)) is 1.0 because the wire has no decimal.
    assert tick_from_wire(wire_number(round_price(1985, 3))) == 1.0
    inferred = book_tick(1985.0, 1986.9, 3, bid_px="1985.00", ask_px="1986.90", mid=1985.95)
    assert abs(inferred - 0.1) < 1e-12


def test_floor_ceil_to_tick_wide_anth_example():
    tick = 0.1
    mid = (1985.0 + 1986.9) / 2.0
    assert floor_to_tick(mid - tick, tick) == 1985.8
    assert ceil_to_tick(mid + tick, tick) == 1986.1
