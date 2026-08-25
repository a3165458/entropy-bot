"""Single official Hyperliquid WebSocket, multiplexing both EntropyIO coins."""

from __future__ import annotations

import json
import logging
import threading
import time
from typing import Any, Callable

import websocket

from entropy_bot.coins import assert_no_foreign_venue, assert_tradable

log = logging.getLogger("entropy_bot.ws")

BookCallback = Callable[[str, dict[str, Any]], None]


class BookFeed:
    def __init__(self, ws_url: str, coins: tuple[str, ...], on_book: BookCallback) -> None:
        self.ws_url = ws_url
        self.coins = tuple(assert_tradable(c) for c in coins)
        self.on_book = on_book
        self.books: dict[str, dict[str, Any]] = {}
        self._stop = threading.Event()
        self._ready = threading.Event()
        self._ws: websocket.WebSocketApp | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        assert_no_foreign_venue({"coins": list(self.coins)})
        self._ws = websocket.WebSocketApp(
            self.ws_url,
            on_open=self._on_open,
            on_message=self._on_message,
            on_error=self._on_error,
            on_close=self._on_close,
        )
        self._thread = threading.Thread(target=self._run, name="hl-ws", daemon=True)
        self._thread.start()
        ping = threading.Thread(target=self._ping_loop, name="hl-ws-ping", daemon=True)
        ping.start()

    def wait_ready(self, timeout: float = 15.0) -> bool:
        return self._ready.wait(timeout)

    def stop(self) -> None:
        self._stop.set()
        if self._ws is not None:
            self._ws.close()

    def _run(self) -> None:
        assert self._ws is not None
        while not self._stop.is_set():
            self._ws.run_forever()
            if self._stop.is_set():
                break
            time.sleep(2.0)

    def _ping_loop(self) -> None:
        while not self._stop.wait(50):
            try:
                if self._ws and self._ws.sock and self._ws.sock.connected:
                    self._ws.send(json.dumps({"method": "ping"}))
            except Exception as exc:  # noqa: BLE001
                log.debug("ws ping failed: %s", exc)

    def _on_open(self, ws: websocket.WebSocketApp) -> None:
        for coin in self.coins:
            sub = {"method": "subscribe", "subscription": {"type": "l2Book", "coin": coin}}
            assert_no_foreign_venue(sub)
            ws.send(json.dumps(sub))
            log.info("subscribed l2Book %s", coin)
        self._ready.set()

    def _on_message(self, _ws: websocket.WebSocketApp, message: str) -> None:
        if message == "Websocket connection established.":
            return
        try:
            payload = json.loads(message)
        except json.JSONDecodeError:
            return
        if payload.get("channel") == "pong":
            return
        if payload.get("channel") != "l2Book":
            return
        data = payload.get("data") or {}
        coin = data.get("coin")
        if not coin:
            return
        try:
            coin = assert_tradable(coin)
        except Exception:
            return
        self.books[coin] = data
        self.on_book(coin, data)

    def _on_error(self, _ws: websocket.WebSocketApp, error: Any) -> None:
        log.warning("ws error: %s", error)

    def _on_close(self, *_args: Any) -> None:
        if not self._stop.is_set():
            log.warning("ws closed; will reconnect")
            self._ready.clear()
