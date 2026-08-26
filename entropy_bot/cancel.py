from __future__ import annotations

import logging

from entropy_bot.coins import ALLOWED_DEX
from entropy_bot.config import Settings, require_signer
from entropy_bot.orders import LiveSigner, is_bot_cloid
from entropy_bot.rest import InfoClient
from entropy_bot.status import load_io_markets

log = logging.getLogger("entropy_bot.cancel")


def run_cancel(settings: Settings) -> int:
    require_signer(settings)
    signer = LiveSigner(settings.private_key or "", settings.account)
    client = InfoClient(settings.api_url)
    try:
        _meta, markets, _ctxs = load_io_markets(client, settings.coins)
        orders = client.open_orders(signer.account, ALLOWED_DEX, optional=True)
        if orders is None:
            log.error("frontendOpenOrders null/429; not assuming the book is empty")
            return 2
        pairs: list[tuple[int, str]] = []
        for order in orders:
            coin = order.get("coin")
            cloid = order.get("cloid") or order.get("cloidStr")
            if coin not in markets:
                continue
            if not is_bot_cloid(cloid):
                continue
            pairs.append((markets[coin].asset_id, cloid))
        if not pairs:
            log.info("no bot cloIDs resting on %s", ",".join(settings.coins))
            return 0
        resp = client.post_exchange(signer.signed_cancel_cloids(pairs))
        log.info("canceled %s bot orders: %s", len(pairs), resp)
        print(resp)
        return 0
    finally:
        client.close()
