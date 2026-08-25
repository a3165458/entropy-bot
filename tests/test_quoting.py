from __future__ import annotations

from entropy_bot.quoting import (
    PaperState,
    QuoteIntent,
    book_top,
    desired_quotes,
    detect_paper_fills,
    install_quotes,
)


def test_paper_fill_when_ask_crosses_bid(markets):
    state = PaperState()
    install_quotes(
        state,
        [
            QuoteIntent("io:ANTH", 200_001, True, 1979.0, 0.02, "B"),
            QuoteIntent("io:ANTH", 200_001, False, 1981.0, 0.02, "A"),
        ],
    )
    top = book_top(
        "io:ANTH",
        {
            "levels": [
                [{"px": "1981.2", "sz": "1", "n": 1}],
                [{"px": "1981.5", "sz": "1", "n": 1}],
            ]
        },
    )
    fills = detect_paper_fills(state, top)
    assert len(fills) == 1
    assert fills[0].side == "A"
    assert state.position["io:ANTH"] == -0.02


def test_desired_quotes_stay_post_only(markets):
    top = book_top(
        "io:ANTH",
        {
            "levels": [
                [{"px": "1979.4", "sz": "1", "n": 1}],
                [{"px": "1979.5", "sz": "1", "n": 1}],
            ]
        },
    )
    intents = desired_quotes(markets["io:ANTH"], top, notional_usd=50, offset_ticks=2)
    bid = next(i for i in intents if i.is_buy)
    ask = next(i for i in intents if not i.is_buy)
    assert bid.px < top.ask
    assert ask.px > top.bid
    assert bid.asset_id == 200_001
