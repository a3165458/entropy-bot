from __future__ import annotations

import logging
import signal
import time

from entropy_bot.coins import ALLOWED_DEX, perp_dex_info
from entropy_bot.config import Settings
from entropy_bot.fees import (
    ALO_TIF_NOTE,
    alo_fill_rebate_usd,
    describe_fee_model,
    estimate_from_settings,
    fee_for_notional,
)
from entropy_bot.quoting import (
    PaperState,
    book_top,
    desired_quotes,
    detect_paper_fills,
    install_quotes,
)
from entropy_bot.rest import InfoClient
from entropy_bot.status import load_io_markets
from entropy_bot.ws import BookFeed

log = logging.getLogger("entropy_bot.paper")


def run_paper(settings: Settings, *, seconds: float | None = None) -> int:
    client = InfoClient(settings.api_url)
    try:
        _meta, markets, _ctxs = load_io_markets(client, settings.coins)
        dex = perp_dex_info(client.perp_dexs(), ALLOWED_DEX)
        log.info(
            "paper mode (no signing)  dex=%s/%s  coins=%s  notional=$%s  offset=%s ticks  isolated-only",
            dex.get("name"),
            dex.get("fullName"),
            ",".join(settings.coins),
            settings.quote_notional_usd,
            settings.quote_offset_ticks,
        )
        log.info("%s", describe_fee_model(settings))
        log.info("%s", ALO_TIF_NOTE)
        for market in markets.values():
            log.info("%s asset=%s  %s", market.coin, market.asset_id, estimate_from_settings(market, settings).summary())
        if settings.account:
            state = client.clearinghouse_state(settings.account, ALLOWED_DEX)
            log.info("isolated snapshot keys=%s", list(state)[:8])
    finally:
        client.close()

    paper = PaperState()
    stop = {"flag": False}

    def on_book(coin: str, data: dict) -> None:
        market = markets[coin]
        top = book_top(coin, data)
        fills = detect_paper_fills(paper, top)
        for fill in fills:
            notional = fill.px * fill.sz
            fee = estimate_from_settings(market, settings)
            maker_usd = fee_for_notional(fee, notional, maker=True)
            if settings.maker_rebate_bps is not None:
                credit = alo_fill_rebate_usd(notional, settings.maker_rebate_bps)
                rebate_txt = (
                    f"ALO rebate credit ${credit:.6f} "
                    f"({settings.maker_rebate_bps:g} bp) maker all-in {fee.maker_pct:.4f}%"
                )
            else:
                rebate_txt = f"ALO maker all-in {fee.maker_pct:.4f}% (${maker_usd:.6f}, no extra rebate)"
            log.info(
                "PAPER FILL %s %s sz=%s px=%s notional=$%.2f (%s) pos=%s  %s  %s",
                fill.coin,
                "BUY" if fill.side == "B" else "SELL",
                fill.sz,
                fill.px,
                notional,
                fill.reason,
                paper.position.get(fill.coin),
                rebate_txt,
                ALO_TIF_NOTE,
            )
        intents = desired_quotes(
            market,
            top,
            notional_usd=settings.quote_notional_usd,
            offset_ticks=settings.quote_offset_ticks,
        )
        if not intents:
            return
        prev = paper.quotes.get(coin) or {}
        changed = any(
            prev.get(intent.side) is None
            or prev[intent.side].px != intent.px
            or prev[intent.side].sz != intent.sz
            for intent in intents
        )
        install_quotes(paper, intents)
        if not changed and not fills:
            return
        bid = next(i for i in intents if i.is_buy)
        ask = next(i for i in intents if not i.is_buy)
        log.info(
            "PAPER ALO %s bid %s x %s | ask %s x %s | book %s/%s spread=%.2f bps",
            coin,
            bid.px,
            bid.sz,
            ask.px,
            ask.sz,
            top.bid,
            top.ask,
            top.spread_bps or -1,
        )

    feed = BookFeed(settings.ws_url, settings.coins, on_book)
    feed.start()
    if not feed.wait_ready():
        log.error("websocket did not become ready")
        feed.stop()
        return 1

    def _handle(_signum: int, _frame: object) -> None:
        stop["flag"] = True

    signal.signal(signal.SIGINT, _handle)
    signal.signal(signal.SIGTERM, _handle)
    deadline = time.time() + seconds if seconds else None
    log.info("paper quoting on one WS connection; Ctrl-C to stop")
    while not stop["flag"]:
        if deadline and time.time() >= deadline:
            break
        time.sleep(0.25)
    feed.stop()
    log.info("paper fills=%s positions=%s", len(paper.fills), paper.position)
    return 0
