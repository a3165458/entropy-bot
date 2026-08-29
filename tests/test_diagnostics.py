from __future__ import annotations

import json
from datetime import datetime
from zoneinfo import ZoneInfo

from entropy_bot.diagnostics import (
    ANTH,
    SNDK,
    BookSnap,
    FillDiagnostics,
    fill_rate,
    markout_bps,
    normalize_diag_coin,
    normalize_side,
    sndk_session,
)
from entropy_bot.ws import BookFeed

NY = ZoneInfo("America/New_York")


def _snap(mid: float, *, age: float = 0.1, spread: float = 0.2) -> BookSnap:
    bid = mid - spread / 2.0
    ask = mid + spread / 2.0
    return BookSnap(mid=mid, spread=spread, spread_bps=(spread / mid) * 10_000.0, age_s=age)


def test_markout_sign_buy_up_sell_down_positive():
    assert markout_bps("buy", 100.0, 101.0) == 100.0
    assert markout_bps("sell", 100.0, 99.0) == 100.0
    assert markout_bps("B", 200.0, 201.0) == 50.0
    assert markout_bps("A", 200.0, 199.0) == 50.0


def test_markout_sign_adverse_is_negative():
    assert markout_bps("buy", 100.0, 99.0) == -100.0
    assert markout_bps("sell", 100.0, 101.0) == -100.0


def test_fill_rate_guards_divide_by_zero():
    assert fill_rate(0, 0) is None
    assert fill_rate(3, 0) is None
    assert fill_rate(1, 4) == 0.25


def test_anth_sndk_counters_never_mix():
    diag = FillDiagnostics((ANTH, SNDK))
    anth = diag.note_quotes(ANTH, 2)
    sndk = diag.note_quotes(SNDK, 3, when=datetime(2024, 3, 13, 10, 0, tzinfo=NY))
    assert anth is not None and anth["coin"] == ANTH
    assert "session" not in anth
    assert anth["quotes"] == 2
    assert anth["fills"] == 0
    assert sndk is not None and sndk["coin"] == SNDK
    assert sndk["session"] == "rth"
    assert sndk["quotes"] == 3
    assert diag.snapshot(ANTH)["quotes"] == 2
    assert diag.snapshot(ANTH)["fills"] == 0
    assert diag.snapshot(SNDK, "rth")["quotes"] == 3
    assert diag.snapshot(SNDK, "ah")["quotes"] == 0
    assert diag.snapshot(SNDK, "rth")["fills"] == 0


def test_quote_vs_fill_counters_and_rate():
    diag = FillDiagnostics((ANTH, SNDK))
    diag.note_quotes(ANTH, 4)
    books = {ANTH: _snap(1986.0)}
    diag.ingest_fills(
        [
            {"coin": "io:ANTH", "side": "B", "px": "1985.8", "tid": 11},
            {"coin": "ANTH", "side": "sell", "px": "1986.2", "tid": 12},
        ],
        lambda coin: books[coin],
        now=1_000.0,
    )
    anth = diag.snapshot(ANTH)
    assert anth["quotes"] == 4
    assert anth["fills"] == 2
    assert anth["fill_rate"] == 0.5
    assert diag.snapshot(SNDK, "rth")["fills"] == 0
    assert diag.snapshot(SNDK, "ah")["fills"] == 0


def test_sndk_rth_ah_frozen_new_york_timestamp():
    rth = datetime(2024, 3, 13, 14, 30, tzinfo=NY)  # Wed
    open_bell = datetime(2024, 3, 13, 9, 30, tzinfo=NY)
    before_open = datetime(2024, 3, 13, 9, 29, tzinfo=NY)
    close_bell = datetime(2024, 3, 13, 16, 0, tzinfo=NY)
    saturday = datetime(2024, 3, 16, 12, 0, tzinfo=NY)
    utc_rth = datetime(2024, 3, 13, 18, 30, tzinfo=ZoneInfo("UTC"))  # 14:30 EDT

    assert sndk_session(rth) == "rth"
    assert sndk_session(open_bell) == "rth"
    assert sndk_session(before_open) == "ah"
    assert sndk_session(close_bell) == "ah"
    assert sndk_session(saturday) == "ah"
    assert sndk_session(utc_rth) == "rth"

    diag = FillDiagnostics((ANTH, SNDK))
    diag.note_quotes(SNDK, 1, when=rth)
    diag.note_quotes(SNDK, 2, when=saturday)
    books = {SNDK: _snap(50.0)}
    diag.ingest_fills(
        [{"coin": "io:SNDK", "side": "B", "px": "50.0", "tid": 21}],
        lambda coin: books[coin],
        now=10.0,
        when=rth,
    )
    diag.ingest_fills(
        [{"coin": "io:SNDK", "side": "A", "px": "50.1", "tid": 22}],
        lambda coin: books[coin],
        now=11.0,
        when=saturday,
    )
    rth_row = diag.snapshot(SNDK, "rth")
    ah_row = diag.snapshot(SNDK, "ah")
    assert rth_row["session"] == "rth"
    assert ah_row["session"] == "ah"
    assert rth_row["quotes"] == 1
    assert rth_row["fills"] == 1
    assert rth_row["fill_rate"] == 1.0
    assert ah_row["quotes"] == 2
    assert ah_row["fills"] == 1
    assert ah_row["fill_rate"] == 0.5
    assert "session" not in diag.snapshot(ANTH)


def test_plus_3s_markout_and_stale_book_skips():
    diag = FillDiagnostics((ANTH, SNDK))
    diag.note_quotes(ANTH, 1)
    state = {"mid": 100.0, "age": 0.2}
    peek = lambda _coin: _snap(state["mid"], age=state["age"])
    diag.ingest_fills(
        [{"coin": "io:ANTH", "side": "buy", "px": "100.0", "tid": 31}],
        peek,
        now=5.0,
    )
    assert diag.flush_markouts(peek, now=7.9) == []
    rows = diag.flush_markouts(peek, now=8.0)
    assert len(rows) == 1
    assert rows[0]["type"] == "fill"
    assert rows[0]["coin"] == ANTH
    assert rows[0]["side"] == "buy"
    assert rows[0]["fill_px"] == 100.0
    assert rows[0]["mid_at_fill"] == 100.0
    assert rows[0]["mid_3s"] == 100.0
    assert rows[0]["markout_bps"] == 0.0
    assert rows[0]["spread"] == 0.2
    assert rows[0]["quotes"] == 1
    assert rows[0]["fills"] == 1

    diag.ingest_fills(
        [{"coin": "io:ANTH", "side": "sell", "px": "100.0", "tid": 32}],
        peek,
        now=20.0,
    )
    state["mid"] = 99.0
    good = diag.flush_markouts(peek, now=23.0)
    assert good[0]["markout_bps"] == 100.0
    assert good[0]["mid_3s"] == 99.0

    state["mid"] = 100.0
    state["age"] = 0.2
    diag.ingest_fills(
        [{"coin": "io:ANTH", "side": "buy", "px": "100.0", "tid": 33}],
        peek,
        now=40.0,
    )
    state["age"] = 16.0
    stale = diag.flush_markouts(peek, now=43.0)
    assert stale[0]["mid_3s"] is None
    assert stale[0]["markout_bps"] is None
    assert stale[0]["mid_at_fill"] == 100.0


def test_prime_and_dedup_do_not_count_snapshot_fills():
    diag = FillDiagnostics((ANTH, SNDK))
    peek = lambda _c: _snap(10.0)
    hist = [{"coin": "io:ANTH", "side": "B", "px": "10", "tid": 99}]
    assert diag.ingest_fills(hist, peek, prime=True, now=1.0) == []
    assert diag.snapshot(ANTH)["fills"] == 0
    assert diag.ingest_fills(hist, peek, prime=False, now=2.0) == []
    assert diag.snapshot(ANTH)["fills"] == 0
    live = [{"coin": "io:ANTH", "side": "B", "px": "10", "tid": 100}]
    assert len(diag.ingest_fills(live, peek, now=3.0)) == 1
    assert diag.snapshot(ANTH)["fills"] == 1


def test_sndk_fill_time_tags_session_and_keeps_markout_buckets_apart():
    diag = FillDiagnostics((ANTH, SNDK))
    rth_ms = int(datetime(2024, 3, 13, 10, 0, tzinfo=NY).timestamp() * 1000)
    ah_ms = int(datetime(2024, 3, 13, 20, 0, tzinfo=NY).timestamp() * 1000)
    peek = lambda _c: _snap(50.0)
    diag.ingest_fills(
        [{"coin": "io:SNDK", "side": "B", "px": "50", "tid": 41, "time": rth_ms}],
        peek,
        now=1.0,
    )
    diag.ingest_fills(
        [{"coin": "io:SNDK", "side": "B", "px": "50", "tid": 42, "time": ah_ms}],
        peek,
        now=2.0,
    )
    rth_fill, ah_fill = diag.flush_markouts(peek, now=5.0)
    assert rth_fill["session"] == "rth"
    assert ah_fill["session"] == "ah"
    assert rth_fill["markout_bps"] == 0.0
    assert ah_fill["markout_bps"] == 0.0
    rth_avg = diag.snapshot(SNDK, "rth")["markout_avg_bps"]
    ah_avg = diag.snapshot(SNDK, "ah")["markout_avg_bps"]
    assert rth_avg == 0.0
    assert ah_avg == 0.0
    assert diag.snapshot(SNDK, "rth")["fills"] == 1
    assert diag.snapshot(SNDK, "ah")["fills"] == 1
    # No untagged SNDK combined bucket that would mix AH into RTH.
    assert all(row.get("session") in {"rth", "ah"} for row in diag.rows if row.get("coin") == SNDK)


def test_maker_flatten_fill_records_plus_3s_markout():
    """FILL_DIAG must still log maker flatten fills (there is no IOC flatten)."""
    diag = FillDiagnostics((ANTH, SNDK))
    quoted = diag.note_quotes(ANTH, 1)
    assert quoted is not None and quoted["quotes"] == 1
    state = {"mid": 1986.0, "age": 0.1}
    peek = lambda _coin: _snap(state["mid"], age=state["age"], spread=1.9)
    pending = diag.ingest_fills(
        [{"coin": "io:ANTH", "side": "A", "px": "1986.1", "tid": 501}],
        peek,
        now=10.0,
    )
    assert len(pending) == 1
    assert pending[0].side == "sell"
    assert pending[0].fill_px == 1986.1
    assert pending[0].mid_at_fill == 1986.0
    rows = diag.flush_markouts(peek, now=13.0)
    assert len(rows) == 1
    row = rows[0]
    assert row["type"] == "fill"
    assert row["coin"] == ANTH
    assert row["side"] == "sell"
    assert row["fill_px"] == 1986.1
    assert row["mid_at_fill"] == 1986.0
    assert row["mid_3s"] == 1986.0
    assert row["markout_bps"] == (1986.1 - 1986.0) / 1986.1 * 10_000.0
    assert row["spread"] == 1.9
    assert row["quotes"] == 1
    assert row["fills"] == 1
    assert row["fill_rate"] == 1.0


def test_foreign_or_unknown_coins_ignored():
    diag = FillDiagnostics((ANTH, SNDK))
    peek = lambda _c: _snap(1.0)
    diag.ingest_fills(
        [
            {"coin": "xyz:SNDK", "side": "B", "px": "1", "tid": 1},
            {"coin": "BTC", "side": "B", "px": "1", "tid": 2},
        ],
        peek,
        now=1.0,
    )
    assert diag.snapshot(ANTH)["fills"] == 0
    assert diag.snapshot(SNDK, "rth")["fills"] == 0
    assert normalize_diag_coin("xyz:SNDK") is None
    assert normalize_side("Q") is None


def test_bookfeed_userfills_does_not_drop_l2book():
    books: list[str] = []
    fills: list[dict] = []
    feed = BookFeed(
        "wss://api.hyperliquid.xyz/ws",
        (ANTH, SNDK),
        lambda coin, _data: books.append(coin),
        user="0x" + "ab" * 20,
        on_user_fills=lambda data: fills.append(data),
    )
    feed._on_message(
        None,  # type: ignore[arg-type]
        json.dumps(
            {
                "channel": "l2Book",
                "data": {
                    "coin": "io:ANTH",
                    "levels": [[{"px": "1", "sz": "1", "n": 1}], [{"px": "2", "sz": "1", "n": 1}]],
                },
            }
        ),
    )
    feed._on_message(
        None,  # type: ignore[arg-type]
        json.dumps(
            {
                "channel": "userFills",
                "data": {
                    "isSnapshot": True,
                    "fills": [{"coin": "io:ANTH", "px": "1", "side": "B", "tid": 1}],
                },
            }
        ),
    )
    assert books == [ANTH]
    assert fills and fills[0]["isSnapshot"] is True
    latest = feed.latest(ANTH)
    assert latest is not None
