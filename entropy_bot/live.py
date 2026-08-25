from __future__ import annotations

import logging
import signal
import time
from typing import Any

from entropy_bot.coins import ALLOWED_DEX, Market, perp_dex_info
from entropy_bot.config import Settings, live_notional, require_live
from entropy_bot.fees import estimate_market_fees, fee_for_notional
from entropy_bot.orders import LiveSigner, format_fee_banner, is_bot_cloid
from entropy_bot.quoting import QuoteIntent, book_top, desired_quotes
from entropy_bot.rest import InfoClient
from entropy_bot.status import load_io_markets
from entropy_bot.ws import BookFeed

log = logging.getLogger("entropy_bot.live")


class LiveQuoter:
    def __init__(
        self,
        settings: Settings,
        markets: dict[str, Market],
        client: InfoClient,
        signer: LiveSigner,
    ) -> None:
        self.settings = settings
        self.markets = markets
        self.client = client
        self.signer = signer
        self.notional = live_notional(settings)
        self.open: dict[str, dict[str, tuple[str, float, float]]] = {}
        self._last_replace: dict[str, float] = {}

    def bootstrap_isolated(self) -> None:
        for market in self.markets.values():
            fee = estimate_market_fees(market)
            maker_usd = fee_for_notional(fee, self.notional, maker=True)
            print(format_fee_banner(market.coin, fee.summary(), self.notional, maker_usd), flush=True)
            payload = self.signer.signed_update_leverage(market, self.settings.max_leverage)
            resp = self.client.post_exchange(payload)
            log.info("isolated leverage %s -> %s", market.coin, resp)

    def on_book(self, coin: str, data: dict[str, Any]) -> None:
        now = time.time()
        if now - self._last_replace.get(coin, 0.0) < 1.5:
            return
        market = self.markets[coin]
        top = book_top(coin, data)
        intents = desired_quotes(
            market,
            top,
            notional_usd=self.notional,
            offset_ticks=self.settings.quote_offset_ticks,
        )
        if not intents:
            return
        if self._same_as_open(intents):
            return
        self._replace(intents)
        self._last_replace[coin] = now

    def _same_as_open(self, intents: list[QuoteIntent]) -> bool:
        for intent in intents:
            current = (self.open.get(intent.coin) or {}).get(intent.side)
            if current is None or current[1] != intent.px or current[2] != intent.sz:
                return False
        return True

    def _replace(self, intents: list[QuoteIntent]) -> None:
        coin = intents[0].coin
        market = self.markets[coin]
        existing = self.open.get(coin) or {}
        if existing:
            pairs = [(market.asset_id, cloid) for cloid, _px, _sz in existing.values()]
            payload = self.signer.signed_cancel_cloids(pairs)
            resp = self.client.post_exchange(payload)
            log.info("cancel prior ALO %s %s", coin, resp)
        specs = []
        recorded: dict[str, tuple[str, float, float]] = {}
        fee = estimate_market_fees(market)
        maker_usd = fee_for_notional(fee, self.notional, maker=True)
        print(format_fee_banner(coin, fee.summary(), self.notional, maker_usd), flush=True)
        for intent in intents:
            cloid = self.signer.next_cloid(intent.coin, intent.side)  # type: ignore[arg-type]
            specs.append((market, intent.is_buy, intent.px, intent.sz, cloid))
            recorded[intent.side] = (cloid, intent.px, intent.sz)
        payload = self.signer.signed_alo_orders(specs)
        resp = self.client.post_exchange(payload)
        self.open[coin] = recorded
        log.info("live ALO %s %s -> %s", coin, recorded, resp)


def run_live(settings: Settings, *, seconds: float | None = None) -> int:
    require_live(settings)
    signer = LiveSigner(settings.private_key or "", settings.account)
    client = InfoClient(settings.api_url)
    try:
        _meta, markets, _ctxs = load_io_markets(client, settings.coins)
        dex = perp_dex_info(client.perp_dexs(), ALLOWED_DEX)
        log.info(
            "LIVE isolated quoting  dex=%s/%s  account=%s  notional=$%s",
            dex.get("name"),
            dex.get("fullName"),
            signer.account,
            live_notional(settings),
        )
        user_state = client.clearinghouse_state(signer.account, ALLOWED_DEX)
        log.info("isolated user state withdrawable=%s", user_state.get("withdrawable"))
        quoter = LiveQuoter(settings, markets, client, signer)
        quoter.bootstrap_isolated()

        stop = {"flag": False}

        def _handle(_signum: int, _frame: object) -> None:
            stop["flag"] = True

        signal.signal(signal.SIGINT, _handle)
        signal.signal(signal.SIGTERM, _handle)

        feed = BookFeed(settings.ws_url, settings.coins, quoter.on_book)
        feed.start()
        if not feed.wait_ready():
            log.error("websocket did not become ready")
            feed.stop()
            return 1
        deadline = time.time() + seconds if seconds else None
        while not stop["flag"]:
            if deadline and time.time() >= deadline:
                break
            time.sleep(0.25)
        feed.stop()
        leftover = [
            (markets[coin].asset_id, cloid)
            for coin, sides in quoter.open.items()
            for cloid, _px, _sz in sides.values()
            if is_bot_cloid(cloid)
        ]
        if leftover:
            resp = client.post_exchange(signer.signed_cancel_cloids(leftover))
            log.info("shutdown cancel %s", resp)
        return 0
    finally:
        client.close()
