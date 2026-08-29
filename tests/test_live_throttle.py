"""Write-throttle: MIN_REPLACE_S, request-weight backoff, maker-only flatten."""

from __future__ import annotations

from typing import Any

from entropy_bot.config import Settings
from entropy_bot.diagnostics import FillDiagnostics
from entropy_bot.errors import RequestWeightLimited, is_weight_limit_error
from entropy_bot.live import (
    LiveQuoter,
    RestSlot,
    deadman_needs_refresh,
    replace_allowed,
    weight_backoff_s,
)
from entropy_bot.quoting import FLAT_STALE_MS, book_top


WEIGHT_ERR = (
    "Too many cumulative requests sent (15401 > 14642) for cumulative volume "
    "traded $4643. Place taker orders to free up 1 request per USDC traded."
)


def _settings(**kwargs) -> Settings:
    base = dict(
        live=True,
        private_key="0x" + "ab" * 32,
        account="0x" + "11" * 20,
        coins=("io:ANTH",),
        quote_notional_usd=50.0,
        quote_offset_ticks=2,
        max_leverage=2,
        api_url="https://api.hyperliquid.xyz",
        ws_url="wss://api.hyperliquid.xyz/ws",
        min_replace_s=12.0,
    )
    base.update(kwargs)
    return Settings(**base)


class Clock:
    def __init__(self, t: float = 1_000_000.0) -> None:
        self.t = t

    def time(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


class FakeSigner:
    account = "0x" + "11" * 20
    signer_address = "0x" + "ab" * 20
    is_agent = True
    _n = 0

    def next_cloid(self, coin: str, side: str) -> str:
        self._n += 1
        return f"0x45424f540000000000000000000000{self._n:02x}"

    def signed_orders(self, specs: list) -> dict[str, Any]:
        return {"action": {"type": "order", "orders": specs}}

    def signed_cancel_oids(self, pairs: list) -> dict[str, Any]:
        return {"action": {"type": "cancel", "cancels": pairs}}

    def signed_cancel_cloids(self, pairs: list) -> dict[str, Any]:
        return {"action": {"type": "cancelByCloid", "cancels": pairs}}

    def signed_schedule_cancel(self, time_ms: int | None) -> dict[str, Any]:
        action: dict[str, Any] = {"type": "scheduleCancel"}
        if time_ms is not None:
            action["time"] = time_ms
        return {"action": action}

    def signed_update_leverage(self, market: object, leverage: int) -> dict[str, Any]:
        return {"action": {"type": "updateLeverage", "leverage": leverage}}


class FakeClient:
    def __init__(self, *, exchange_error: object | None = None) -> None:
        self.posts: list[dict[str, Any]] = []
        self.exchange_error = exchange_error
        self._oid = 3_000_000_000

    def post_exchange(self, payload: dict[str, Any]) -> Any:
        if self.exchange_error is not None:
            err = self.exchange_error
            self.exchange_error = None
            if isinstance(err, Exception):
                raise err
            return err
        self.posts.append(payload)
        action = (payload or {}).get("action") or {}
        kind = action.get("type")
        if kind == "order":
            n = len(action.get("orders") or payload.get("action", {}).get("orders") or [])
            if not n:
                specs = action.get("orders")
                n = len(specs) if isinstance(specs, list) else 2
            statuses = []
            for _ in range(max(n, 1)):
                self._oid += 1
                statuses.append({"resting": {"oid": self._oid}})
            return {"status": "ok", "response": {"type": "order", "data": {"statuses": statuses}}}
        return {"status": "ok", "response": {"type": kind or "ok"}}

    def clearinghouse_state(self, *args: object, **kwargs: object) -> None:
        return None

    def open_orders(self, *args: object, **kwargs: object) -> list:
        return []

    def l2_book(self, coin: str) -> dict[str, Any]:
        return _book_data("1985.0", "1985.1")

    def user_fills(self, *args: object, **kwargs: object) -> list:
        return []

    def extra_agents(self, *args: object, **kwargs: object) -> list:
        return []


def _book_data(bid: str, ask: str) -> dict[str, Any]:
    return {"levels": [[{"px": bid, "sz": "1", "n": 1}], [{"px": ask, "sz": "1", "n": 1}]]}


def _top(bid: str, ask: str, coin: str = "io:ANTH"):
    return book_top(coin, _book_data(bid, ask))


def _quoter(markets, *, clock: Clock, client: FakeClient | None = None, **kw) -> LiveQuoter:
    client = client or FakeClient()
    q = LiveQuoter(_settings(**kw), {"io:ANTH": markets["io:ANTH"]}, client, FakeSigner())
    q.deadman_until = clock.t + 100.0
    q.last_deadman = clock.t
    return q


def _order_posts(client: FakeClient) -> list[dict[str, Any]]:
    out = []
    for payload in client.posts:
        action = payload.get("action") or {}
        if action.get("type") == "order":
            out.append(payload)
    return out


def test_replace_allowed_helpers():
    assert replace_allowed(has_rest=True, elapsed_s=0.5, min_replace_s=12) is False
    assert replace_allowed(has_rest=True, elapsed_s=12.0, min_replace_s=12) is True
    assert replace_allowed(has_rest=False, elapsed_s=0.5, min_replace_s=12) is True
    assert weight_backoff_s(12) == 30.0
    assert weight_backoff_s(45) == 45.0
    assert deadman_needs_refresh(0.0, 100.0) is True
    assert deadman_needs_refresh(120.0, 100.0) is False  # 20s remaining
    assert deadman_needs_refresh(107.0, 100.0) is True  # 7s remaining


def test_is_weight_limit_error_matches_hyperliquid_text():
    assert is_weight_limit_error(WEIGHT_ERR)
    assert is_weight_limit_error({"status": "err", "response": WEIGHT_ERR})
    assert is_weight_limit_error(RequestWeightLimited(WEIGHT_ERR))
    assert is_weight_limit_error("request weight exceeded")
    assert not is_weight_limit_error("Bad Alo Px")
    assert not is_weight_limit_error({"status": "ok"})


def test_min_replace_interval_suppresses_second_place(markets, monkeypatch):
    clock = Clock()
    monkeypatch.setattr("entropy_bot.live.time.time", clock.time)
    client = FakeClient()
    quoter = _quoter(markets, clock=clock, client=client)

    quoter.requote("io:ANTH", _top("1985.0", "1985.1"))
    first = len(_order_posts(client))
    assert first == 1
    assert quoter._has_rest("io:ANTH")

    clock.advance(3.0)
    quoter.requote("io:ANTH", _top("1985.4", "1985.5"))
    assert len(_order_posts(client)) == first
    assert quoter.rests["io:ANTH"]["B"].px == 1985.0

    clock.advance(10.0)  # total 13s >= 12
    quoter.requote("io:ANTH", _top("1985.4", "1985.5"))
    assert len(_order_posts(client)) == first + 1


def test_weight_limit_error_triggers_backoff(markets, monkeypatch):
    clock = Clock()
    monkeypatch.setattr("entropy_bot.live.time.time", clock.time)
    client = FakeClient(exchange_error={"status": "err", "response": WEIGHT_ERR})
    quoter = _quoter(markets, clock=clock, client=client)

    quoter.requote("io:ANTH", _top("1985.0", "1985.1"))
    assert quoter._in_weight_backoff()
    assert quoter._weight_logged is True
    posts_after_error = len(client.posts)

    clock.advance(5.0)
    quoter.requote("io:ANTH", _top("1985.4", "1985.5"))
    assert len(client.posts) == posts_after_error
    assert quoter._in_weight_backoff()

    clock.advance(26.0)  # 31s total >= 30s floor
    assert not quoter._in_weight_backoff()
    quoter.requote("io:ANTH", _top("1985.4", "1985.5"))
    assert len(_order_posts(client)) >= 1


def _spec_tif_ro(first_spec: object) -> tuple[str, bool]:
    if isinstance(first_spec, tuple):
        return str(first_spec[-1]), bool(first_spec[-2])
    tif = first_spec.get("tif") or (first_spec.get("t") or {}).get("limit", {}).get("tif")
    reduce_only = first_spec.get("r") or first_spec.get("reduce_only")
    return str(tif), bool(reduce_only)


def test_flatten_stays_alo_and_respects_min_replace(markets, monkeypatch):
    clock = Clock()
    monkeypatch.setattr("entropy_bot.live.time.time", clock.time)
    client = FakeClient()
    quoter = _quoter(markets, clock=clock, client=client)

    quoter.requote("io:ANTH", _top("1985.0", "1985.1"))
    assert len(_order_posts(client)) == 1
    quoter.rests["io:ANTH"]["A"] = RestSlot(oid=3_000_000_010, px=1985.1, sz=0.02, placed_at=clock.t)
    quoter.last_replace["io:ANTH"] = clock.t
    quoter.pos["io:ANTH"] = 0.02
    quoter.pos_since["io:ANTH"] = clock.t - 16.0

    clock.advance(1.0)
    assert clock.t - quoter.last_replace["io:ANTH"] < 12
    quoter.requote("io:ANTH", _top("1985.0", "1985.1"))
    assert len(_order_posts(client)) == 1, "flatten must not bypass MIN_REPLACE_S with IOC"

    clock.advance(12.0)
    quoter.requote("io:ANTH", _top("1985.0", "1985.1"))
    orders = _order_posts(client)
    assert len(orders) == 2
    last_action = orders[-1]["action"]
    specs = last_action.get("orders") or []
    assert specs, "flatten ALO should place after MIN_REPLACE_S"
    tif, reduce_only = _spec_tif_ro(specs[0])
    assert tif.lower() == "alo"
    assert reduce_only
    blob = str(orders[-1]).lower()
    assert "ioc" not in blob


def test_unfilled_flatten_alo_cancel_replaces_after_stale(markets, monkeypatch):
    clock = Clock()
    monkeypatch.setattr("entropy_bot.live.time.time", clock.time)
    client = FakeClient()
    quoter = _quoter(markets, clock=clock, client=client, min_replace_s=0.0)
    quoter.pos["io:ANTH"] = 0.02
    quoter.pos_since["io:ANTH"] = clock.t - 16.0

    quoter.requote("io:ANTH", _top("1985.0", "1986.9"))
    first = len(_order_posts(client))
    assert first == 1
    tif, reduce_only = _spec_tif_ro(_order_posts(client)[-1]["action"]["orders"][0])
    assert tif.lower() == "alo" and reduce_only
    placed = quoter.quote_placed_at["io:ANTH"]
    assert placed > 0

    clock.advance((FLAT_STALE_MS / 1000.0) - 5.0)
    quoter.requote("io:ANTH", _top("1985.0", "1986.9"))
    assert len(_order_posts(client)) == first

    clock.advance(6.0)
    quoter.requote("io:ANTH", _top("1985.0", "1986.9"))
    orders = _order_posts(client)
    assert len(orders) == first + 1
    tif, reduce_only = _spec_tif_ro(orders[-1]["action"]["orders"][0])
    assert tif.lower() == "alo"
    assert reduce_only
    assert "ioc" not in str(orders[-1]).lower()


def test_flatten_alo_place_counts_quote_and_fill_diag_markout(markets, monkeypatch):
    clock = Clock()
    monkeypatch.setattr("entropy_bot.live.time.time", clock.time)
    client = FakeClient()
    quoter = _quoter(markets, clock=clock, client=client, min_replace_s=0.0)
    quoter.diag = FillDiagnostics(("io:ANTH",))
    quoter.pos["io:ANTH"] = 0.041
    quoter.pos_since["io:ANTH"] = clock.t - 16.0
    top = _top("1985.0", "1986.9")
    quoter.requote("io:ANTH", top)
    assert quoter.diag.snapshot("io:ANTH")["quotes"] == 1
    assert quoter.diag.snapshot("io:ANTH")["fills"] == 0

    quoter.books["io:ANTH"] = (clock.t, _book_data("1985.0", "1986.9"))
    quoter.diag.ingest_fills(
        [{"coin": "io:ANTH", "side": "A", "px": "1986.1", "tid": 8801}],
        quoter.peek_book,
        now=clock.t,
    )
    clock.advance(3.0)
    quoter.books["io:ANTH"] = (clock.t, _book_data("1985.0", "1986.9"))
    rows = quoter.diag.flush_markouts(quoter.peek_book, now=clock.t)
    assert len(rows) == 1
    assert rows[0]["coin"] == "io:ANTH"
    assert rows[0]["side"] == "sell"
    assert rows[0]["fill_px"] == 1986.1
    assert rows[0]["mid_at_fill"] is not None
    assert rows[0]["mid_3s"] is not None
    assert rows[0]["markout_bps"] is not None
    assert rows[0]["quotes"] == 1
    assert rows[0]["fills"] == 1


def test_deadman_refreshes_only_when_remaining_under_8s(markets, monkeypatch):
    clock = Clock()
    monkeypatch.setattr("entropy_bot.live.time.time", clock.time)
    client = FakeClient()
    quoter = _quoter(markets, clock=clock, client=client)
    quoter.deadman_until = clock.t + 20.0
    quoter.last_deadman = clock.t

    quoter.maybe_deadman()
    assert not any((p.get("action") or {}).get("type") == "scheduleCancel" for p in client.posts)

    clock.advance(13.0)  # 7s remaining
    quoter.maybe_deadman()
    cancels = [p for p in client.posts if (p.get("action") or {}).get("type") == "scheduleCancel"]
    assert len(cancels) == 1
