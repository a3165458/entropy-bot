from __future__ import annotations

from entropy_bot.config import Settings
from entropy_bot.status import format_status
from tests.conftest import IO_META, PERP_DEXS
from entropy_bot.coins import perp_dex_info, resolve_markets


def test_status_shows_tier4_both_coins_and_alo():
    markets = resolve_markets(PERP_DEXS, IO_META)
    dex = perp_dex_info(PERP_DEXS, "io")
    ctxs = [
        {},
        {"markPx": "1", "oraclePx": "1", "midPx": "1", "funding": "0", "openInterest": "0", "dayNtlVlm": "0"},
        {"markPx": "1", "oraclePx": "1", "midPx": "1", "funding": "0", "openInterest": "0", "dayNtlVlm": "0"},
        {},
    ]
    books = {
        "io:ANTH": {"levels": [[{"px": "1979.4", "sz": "1", "n": 1}], [{"px": "1979.5", "sz": "1", "n": 1}]]},
        "io:SNDK": {"levels": [[{"px": "1504.3", "sz": "1", "n": 1}], [{"px": "1504.4", "sz": "1", "n": 1}]]},
    }
    settings = Settings(
        live=False,
        private_key=None,
        account=None,
        coins=("io:ANTH", "io:SNDK"),
        quote_notional_usd=50,
        quote_offset_ticks=2,
        max_leverage=2,
        api_url="https://api.hyperliquid.xyz",
        ws_url="wss://api.hyperliquid.xyz/ws",
    )
    text = format_status(dex, markets, ctxs, books, settings=settings)
    assert "io:ANTH" in text and "io:SNDK" in text
    assert "xyz:SNDK" not in text
    assert "vntl:ANTHROPIC" not in text
    assert "Entropy Partner T4" in text
    assert "self rebate 200% of deployer share" in text
    assert "gross taker 0.0056%" in text
    assert "net 0.0000%" in text
    assert "gross maker 0.0000%" in text
    assert "HL volume tier 4" in text
    assert "default TIF=ALO" in text
    assert "growthMode=enabled" in text
    assert "isolated-only" in text
    assert "优惠/referred-user benefit not applied to own fills" in text
