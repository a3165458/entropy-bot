"""Official Hyperliquid HTTP info client. Be polite: cache + small pauses."""

from __future__ import annotations

import time
from typing import Any

import requests

from entropy_bot.coins import ALLOWED_DEX, assert_no_foreign_venue, assert_tradable

DEFAULT_TIMEOUT = 15.0
INFO_PAUSE_S = 0.15


class InfoClient:
    def __init__(self, base_url: str, *, timeout: float = DEFAULT_TIMEOUT) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.session = requests.Session()
        self._perp_dexs: list[Any] | None = None
        self._meta: dict[str, Any] | None = None
        self._meta_and_ctxs: Any | None = None

    def close(self) -> None:
        self.session.close()

    def post_info(self, payload: dict[str, Any], *, cacheable: bool = False) -> Any:
        assert_no_foreign_venue(payload)
        url = f"{self.base_url}/info"
        response = self.session.post(url, json=payload, timeout=self.timeout)
        response.raise_for_status()
        data = response.json()
        if not cacheable:
            time.sleep(INFO_PAUSE_S)
        return data

    def perp_dexs(self, *, refresh: bool = False) -> list[Any]:
        if self._perp_dexs is None or refresh:
            self._perp_dexs = self.post_info({"type": "perpDexs"}, cacheable=True)
        return self._perp_dexs

    def meta(self, dex: str = ALLOWED_DEX, *, refresh: bool = False) -> dict[str, Any]:
        if dex != ALLOWED_DEX:
            raise ValueError(f"this client only queries dex={ALLOWED_DEX!r}")
        if self._meta is None or refresh:
            self._meta = self.post_info({"type": "meta", "dex": dex}, cacheable=True)
        return self._meta

    def meta_and_asset_ctxs(self, dex: str = ALLOWED_DEX, *, refresh: bool = False) -> Any:
        if dex != ALLOWED_DEX:
            raise ValueError(f"this client only queries dex={ALLOWED_DEX!r}")
        if self._meta_and_ctxs is None or refresh:
            self._meta_and_ctxs = self.post_info(
                {"type": "metaAndAssetCtxs", "dex": dex}, cacheable=True
            )
            if isinstance(self._meta_and_ctxs, list) and self._meta_and_ctxs:
                self._meta = self._meta_and_ctxs[0]
        return self._meta_and_ctxs

    def l2_book(self, coin: str) -> dict[str, Any]:
        coin = assert_tradable(coin)
        return self.post_info({"type": "l2Book", "coin": coin})

    def clearinghouse_state(self, user: str, dex: str = ALLOWED_DEX) -> dict[str, Any]:
        if dex != ALLOWED_DEX:
            raise ValueError(f"this client only queries dex={ALLOWED_DEX!r}")
        return self.post_info({"type": "clearinghouseState", "user": user, "dex": dex})

    def open_orders(self, user: str, dex: str = ALLOWED_DEX) -> list[dict[str, Any]]:
        if dex != ALLOWED_DEX:
            raise ValueError(f"this client only queries dex={ALLOWED_DEX!r}")
        return self.post_info({"type": "frontendOpenOrders", "user": user, "dex": dex})

    def post_exchange(self, payload: dict[str, Any]) -> Any:
        assert_no_foreign_venue(payload)
        url = f"{self.base_url}/exchange"
        response = self.session.post(url, json=payload, timeout=self.timeout)
        response.raise_for_status()
        return response.json()
