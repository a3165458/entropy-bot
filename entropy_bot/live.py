"""Official-API live MM. Port of entropy-desk.user.js 1.6.1.

Talks only to https://api.hyperliquid.xyz and wss://api.hyperliquid.xyz/ws.
No Tampermonkey / window.ethereum. Per coin: at most 1 buy + 1 sell.

Dead-man `scheduleCancel` is **account-wide**: when it fires it cancels every
open order on the master account, not just io:ANTH / io:SNDK.
"""

from __future__ import annotations

import logging
import signal
import threading
import time
from dataclasses import dataclass, field
from typing import Any

from entropy_bot.coins import ALLOWED_DEX, Market, perp_dex_info
from entropy_bot.config import Settings, live_notional, require_live
from entropy_bot.fees import (
    ALO_TIF_NOTE,
    alo_fill_rebate_usd,
    describe_fee_model,
    estimate_from_settings,
    fee_for_notional,
)
from entropy_bot.orders import (
    LiveSigner,
    action_ok,
    as_oid,
    format_fee_banner,
    is_alo_reject,
    is_bot_cloid,
    resp_statuses,
    status_error,
    status_oid,
)
from entropy_bot.quoting import (
    STALE_QUOTE_MS,
    BookTop,
    QuotePlan,
    book_top,
    quote_plan,
    sync_pos_since,
)
from entropy_bot.diagnostics import BookSnap, FillDiagnostics, snap_from_top
from entropy_bot.errors import RateLimited
from entropy_bot.rest import InfoClient
from entropy_bot.status import load_io_markets
from entropy_bot.ws import BookFeed

log = logging.getLogger("entropy_bot.live")

WS_STALE_S = 15.0
POS_POLL_S = 0.8
FILL_POLL_S = 2.0
LOOP_S = 0.4
DEADMAN_AHEAD_MS = 20_000
DEADMAN_MIN_MS = 5_000
DEADMAN_FALLBACK_MS = 6_000
DEADMAN_REFRESH_S = 8.0
IOC_GAP_S = 0.4


@dataclass
class RestSlot:
    oid: int | None = None
    cloid: str | None = None
    px: float | None = None
    sz: float | None = None
    tif: str = "Alo"
    reduce_only: bool = False
    placed_at: float = 0.0

    def occupied(self) -> bool:
        return self.oid is not None or self.cloid is not None


def empty_rests(coins: tuple[str, ...]) -> dict[str, dict[str, RestSlot]]:
    return {coin: {"B": RestSlot(), "A": RestSlot()} for coin in coins}


def order_side_buy(order: dict[str, Any]) -> bool | None:
    side = order.get("side")
    if side in {"B", "b"}:
        return True
    if side in {"A", "S", "a", "s"}:
        return False
    if order.get("b") is True:
        return True
    if order.get("b") is False:
        return False
    return None


def match_order_coin(order: dict[str, Any], markets: dict[str, Market]) -> str | None:
    raw = str(order.get("coin") or "")
    if raw.startswith(("xyz:", "vntl:")):
        return None
    if raw in markets:
        return raw
    aid_raw = order.get("a", order.get("asset"))
    aid: int | None
    try:
        aid = int(aid_raw) if aid_raw is not None else None
    except (TypeError, ValueError):
        aid = None
    for coin, market in markets.items():
        base = coin.split(":", 1)[1]
        if raw == base or raw == f"@{market.asset_id}" or raw == str(market.asset_id):
            return coin
        if aid is not None and aid == market.asset_id:
            return coin
    return None


def apply_open_orders(
    rests: dict[str, dict[str, RestSlot]],
    extras: dict[str, list[int]],
    opens: list[dict[str, Any]] | None,
    markets: dict[str, Market],
) -> tuple[dict[str, dict[str, RestSlot]], dict[str, list[int]]]:
    """Rebuild rest cache from frontendOpenOrders. None/429 must not wipe cache."""
    if opens is None:
        log.warning("open-order query null/429; keeping local rest cache")
        return rests, extras
    next_rests = empty_rests(tuple(markets))
    next_extras: dict[str, list[int]] = {coin: [] for coin in markets}
    for order in opens:
        coin = match_order_coin(order, markets)
        if coin is None or coin not in next_rests:
            continue
        oid_raw = order.get("oid", order.get("o"))
        if oid_raw is None:
            continue
        try:
            oid = as_oid(oid_raw)
        except ValueError:
            continue
        is_buy = order_side_buy(order)
        side = "B" if is_buy is True else "A" if is_buy is False else None
        if side is None:
            continue
        slot = next_rests[coin][side]
        cloid = order.get("cloid") or order.get("cloidStr")
        if not slot.occupied():
            slot.oid = oid
            slot.cloid = cloid if isinstance(cloid, str) else None
            try:
                slot.px = float(order.get("limitPx") or order.get("px") or 0) or None
            except (TypeError, ValueError):
                slot.px = None
        else:
            next_extras[coin].append(oid)
    for coin, extra_oids in next_extras.items():
        if extra_oids:
            log.info("extra rests %s oids=%s", coin, extra_oids)
    return next_rests, next_extras


def list_cached_oids(
    rests: dict[str, dict[str, RestSlot]],
    extras: dict[str, list[int]],
    coin: str,
) -> list[int]:
    out: list[int] = []
    seen: set[int] = set()
    for side in ("B", "A"):
        slot = rests.get(coin, {}).get(side)
        if slot and slot.oid is not None and slot.oid not in seen:
            seen.add(slot.oid)
            out.append(slot.oid)
    for oid in extras.get(coin, []):
        if oid not in seen:
            seen.add(oid)
            out.append(oid)
    return out


def list_cached_cloids(rests: dict[str, dict[str, RestSlot]], coin: str) -> list[str]:
    out: list[str] = []
    for side in ("B", "A"):
        slot = rests.get(coin, {}).get(side)
        if slot and slot.cloid:
            out.append(slot.cloid)
    return out


def clear_coin_rests(rests: dict[str, dict[str, RestSlot]], coin: str) -> None:
    rests[coin] = {"B": RestSlot(), "A": RestSlot()}


def positions_from_state(
    state: dict[str, Any] | None,
    coins: tuple[str, ...],
    markets: dict[str, Market],
) -> dict[str, float] | None:
    if state is None:
        return None
    out = {coin: 0.0 for coin in coins}
    rows = state.get("assetPositions")
    if not isinstance(rows, list):
        return None
    for item in rows:
        pos = (item or {}).get("position") or {}
        coin = match_order_coin({"coin": pos.get("coin"), "a": None}, markets)
        if coin is None or coin not in out:
            raw = str(pos.get("coin") or "")
            if raw in out:
                coin = raw
            else:
                continue
        try:
            out[coin] = float(pos.get("szi") or 0)
        except (TypeError, ValueError):
            out[coin] = 0.0
    return out


def deadman_deadline_ms(ahead_ms: int, *, now_ms: int | None = None) -> int:
    now = int(time.time() * 1000) if now_ms is None else now_ms
    t = now + int(ahead_ms)
    if t - now < DEADMAN_MIN_MS:
        t = now + DEADMAN_FALLBACK_MS
    return t


def side_occupied(rests: dict[str, dict[str, RestSlot]], coin: str, side: str) -> bool:
    slot = rests.get(coin, {}).get(side)
    return bool(slot and slot.occupied())


@dataclass
class LiveQuoter:
    settings: Settings
    markets: dict[str, Market]
    client: InfoClient
    signer: LiveSigner
    notional: float = 0.0
    rests: dict[str, dict[str, RestSlot]] = field(default_factory=dict)
    extra_oids: dict[str, list[int]] = field(default_factory=dict)
    pos: dict[str, float] = field(default_factory=dict)
    pos_since: dict[str, float] = field(default_factory=dict)
    last_plan_key: dict[str, str] = field(default_factory=dict)
    quote_placed_at: dict[str, float] = field(default_factory=dict)
    last_take_at: dict[str, float] = field(default_factory=dict)
    last_pos_poll: float = 0.0
    last_fill_poll: float = 0.0
    last_deadman: float = 0.0
    _lock: threading.Lock = field(default_factory=threading.Lock)
    books: dict[str, tuple[float, dict[str, Any]]] = field(default_factory=dict)
    diag: FillDiagnostics | None = None
    _feed: BookFeed | None = None

    def __post_init__(self) -> None:
        if not self.notional:
            self.notional = live_notional(self.settings)
        coins = tuple(self.markets)
        if not self.rests:
            self.rests = empty_rests(coins)
        if not self.extra_oids:
            self.extra_oids = {coin: [] for coin in coins}
        if not self.pos:
            self.pos = {coin: 0.0 for coin in coins}
        if not self.pos_since:
            self.pos_since = {coin: 0.0 for coin in coins}
        if self.diag is None:
            self.diag = FillDiagnostics(coins)

    def on_book(self, coin: str, data: dict[str, Any]) -> None:
        with self._lock:
            self.books[coin] = (time.time(), data)

    def peek_book(self, coin: str) -> BookSnap | None:
        """Cache/WS l2Book only. Diagnostics must not open a WS or REST-fallback."""
        now = time.time()
        ts = 0.0
        data: dict[str, Any] | None = None
        with self._lock:
            if coin in self.books:
                ts, data = self.books[coin]
        feed = self._feed
        if feed is not None:
            latest = feed.latest(coin)
            if latest is not None:
                ts, data = latest
        if data is None:
            return None
        return snap_from_top(book_top(coin, data), now - ts)

    def _diag_quotes(self, coin: str, n: int, *, take: bool) -> None:
        if take or n <= 0 or self.diag is None:
            return
        try:
            self.diag.note_quotes(coin, n)
        except Exception as exc:  # noqa: BLE001
            log.warning("fill-diag quotes: %s", exc)

    def on_user_fills(self, data: dict[str, Any]) -> None:
        if self.diag is None:
            return
        fills = data.get("fills") if isinstance(data, dict) else None
        if not isinstance(fills, list):
            return
        try:
            self.diag.ingest_fills(fills, self.peek_book, prime=bool(data.get("isSnapshot")))
        except Exception as exc:  # noqa: BLE001
            log.warning("fill-diag userFills: %s", exc)

    def poll_user_fills(self, *, force: bool = False) -> None:
        if self.diag is None:
            return
        now = time.time()
        if not force and now - self.last_fill_poll < FILL_POLL_S:
            return
        rows = self.client.user_fills(self.signer.account, optional=True)
        self.last_fill_poll = now
        if rows is None:
            return
        try:
            self.diag.ingest_fills(rows, self.peek_book, prime=not self.diag.primed)
        except Exception as exc:  # noqa: BLE001
            log.warning("fill-diag userFills poll: %s", exc)

    def flush_markouts(self) -> None:
        if self.diag is None:
            return
        try:
            self.diag.flush_markouts(self.peek_book)
        except Exception as exc:  # noqa: BLE001
            log.warning("fill-diag markout: %s", exc)

    def bootstrap_isolated(self) -> None:
        for market in self.markets.values():
            fee = estimate_from_settings(market, self.settings)
            maker_usd = fee_for_notional(fee, self.notional, maker=True)
            rebate_usd = (
                alo_fill_rebate_usd(self.notional, self.settings.maker_rebate_bps)
                if self.settings.maker_rebate_bps is not None
                else None
            )
            print(
                format_fee_banner(market.coin, fee.summary(), self.notional, maker_usd, rebate_usd),
                flush=True,
            )
            payload = self.signer.signed_update_leverage(market, self.settings.max_leverage)
            resp = self.client.post_exchange(payload)
            log.info("isolated leverage %s -> %s", market.coin, resp)

    def _fee_banner(self, coin: str) -> None:
        market = self.markets[coin]
        fee = estimate_from_settings(market, self.settings)
        maker_usd = fee_for_notional(fee, self.notional, maker=True)
        rebate_usd = (
            alo_fill_rebate_usd(self.notional, self.settings.maker_rebate_bps)
            if self.settings.maker_rebate_bps is not None
            else None
        )
        print(format_fee_banner(coin, fee.summary(), self.notional, maker_usd, rebate_usd), flush=True)

    def refresh_positions(self, *, force: bool = False) -> None:
        now = time.time()
        if not force and now - self.last_pos_poll < POS_POLL_S:
            return
        state = self.client.clearinghouse_state(self.signer.account, ALLOWED_DEX, optional=True)
        parsed = positions_from_state(state, tuple(self.markets), self.markets)
        if parsed is None:
            log.warning("clearinghouseState null/429; keeping inventory cache")
            if force:
                return
            self.last_pos_poll = now
            return
        self.pos_since = sync_pos_since(self.pos, parsed, self.pos_since, now)
        self.pos = parsed
        self.last_pos_poll = now

    def book_for(self, coin: str, feed: BookFeed | None) -> BookTop | None:
        now = time.time()
        ts = 0.0
        data: dict[str, Any] | None = None
        with self._lock:
            if coin in self.books:
                ts, data = self.books[coin]
        if feed is not None:
            latest = feed.latest(coin)
            if latest is not None:
                ts, data = latest
        stale = data is None or (now - ts) > WS_STALE_S
        if stale:
            try:
                data = self.client.l2_book(coin)
                ts = time.time()
                with self._lock:
                    self.books[coin] = (ts, data)
            except Exception as exc:  # noqa: BLE001
                log.warning("REST l2Book fallback failed %s: %s", coin, exc)
                if data is None:
                    return None
        return book_top(coin, data or {})

    def plan_for(self, coin: str, top: BookTop) -> QuotePlan:
        now = time.time()
        szi = self.pos.get(coin, 0.0)
        since = self.pos_since.get(coin, 0.0)
        age_ms = int(max(0.0, (now - since) * 1000)) if since and abs(szi) > 0 else 0
        return quote_plan(
            self.markets[coin],
            top,
            szi=szi,
            age_ms=age_ms,
            notional_usd=self.notional,
        )

    def _post(self, payload: dict[str, Any]) -> Any:
        return self.client.post_exchange(payload)

    def schedule_deadman(self, ahead_ms: int = DEADMAN_AHEAD_MS) -> None:
        """Account-wide: cancels ALL open orders on the master when it fires."""
        deadline = deadman_deadline_ms(ahead_ms)
        try:
            resp = self._post(self.signer.signed_schedule_cancel(deadline))
            self.last_deadman = time.time()
            log.info("dead-man scheduleCancel +%ss account-wide -> %s", ahead_ms // 1000, resp)
        except RateLimited:
            fallback = deadman_deadline_ms(DEADMAN_FALLBACK_MS)
            resp = self._post(self.signer.signed_schedule_cancel(fallback))
            self.last_deadman = time.time()
            log.warning("dead-man 429; fallback +%ss account-wide -> %s", DEADMAN_FALLBACK_MS // 1000, resp)

    def maybe_deadman(self) -> None:
        if time.time() - self.last_deadman < DEADMAN_REFRESH_S:
            return
        try:
            self.schedule_deadman(DEADMAN_AHEAD_MS)
        except Exception as exc:  # noqa: BLE001
            log.warning("dead-man renew failed: %s", exc)
            self.last_deadman = time.time() - DEADMAN_REFRESH_S / 2

    def clear_deadman(self) -> None:
        try:
            resp = self._post(self.signer.signed_schedule_cancel(None))
            log.info("dead-man scheduleCancel cleared (no time) -> %s", resp)
        except RateLimited:
            self.schedule_deadman(DEADMAN_FALLBACK_MS)
        except Exception as exc:  # noqa: BLE001
            log.warning("dead-man clear failed: %s", exc)

    def _cancel_pairs(self, coin: str, oids: list[int], cloids: list[str]) -> bool:
        market = self.markets[coin]
        ok = True
        if oids:
            try:
                resp = self._post(self.signer.signed_cancel_oids([(market.asset_id, oid) for oid in oids]))
                log.info("cancel oids %s %s -> %s", coin, oids, resp)
                if not action_ok(resp):
                    ok = False
            except RateLimited:
                log.warning("cancel oids 429 %s", coin)
                raise
            except Exception as exc:  # noqa: BLE001
                log.warning("cancel oids %s: %s", coin, exc)
                ok = False
        if cloids:
            try:
                resp = self._post(self.signer.signed_cancel_cloids([(market.asset_id, c) for c in cloids]))
                log.info("cancel cloids %s -> %s", coin, resp)
            except Exception as exc:  # noqa: BLE001
                log.warning("cancel cloids %s: %s", coin, exc)
                ok = False
        return ok

    def cancel_coin_rests(self, *, refetch: bool = True) -> dict[str, Any]:
        """Cancel cache then refetch frontendOpenOrders dex:io. 429 does not empty cache."""
        limited = False
        for coin in self.markets:
            oids = list_cached_oids(self.rests, self.extra_oids, coin)
            cloids = list_cached_cloids(self.rests, coin)
            if oids or cloids:
                try:
                    self._cancel_pairs(coin, oids, cloids)
                    clear_coin_rests(self.rests, coin)
                    self.extra_oids[coin] = []
                except RateLimited:
                    limited = True
                    log.warning("cancel cache 429 %s; keeping rest cache", coin)
        if not refetch:
            return {"rateLimited": limited}
        opens = self.client.open_orders(self.signer.account, ALLOWED_DEX, optional=True)
        if opens is None:
            log.warning("frontendOpenOrders null/429; not wiping rest cache")
            try:
                self.schedule_deadman(DEADMAN_FALLBACK_MS)
            except Exception as exc:  # noqa: BLE001
                log.warning("429 fallback scheduleCancel failed: %s", exc)
            return {"rateLimited": True, "cachePreserved": True}
        self.rests, self.extra_oids = apply_open_orders(self.rests, self.extra_oids, opens, self.markets)
        for coin in self.markets:
            oids = list_cached_oids(self.rests, self.extra_oids, coin)
            cloids = list_cached_cloids(self.rests, coin)
            if not oids and not cloids:
                continue
            try:
                self._cancel_pairs(coin, oids, cloids)
                clear_coin_rests(self.rests, coin)
                self.extra_oids[coin] = []
            except RateLimited:
                limited = True
        return {"rateLimited": limited, "fetched": True}

    def _place(self, coin: str, plan: QuotePlan) -> dict[str, Any] | None:
        if not plan.intents:
            return None
        specs = []
        pending: list[tuple[str, Any]] = []
        for intent in plan.intents:
            if side_occupied(self.rests, coin, intent.side):
                log.warning("refuse duplicate %s %s; oid/cloid already cached", coin, intent.side)
                continue
            cloid = self.signer.next_cloid(intent.coin, intent.side)  # type: ignore[arg-type]
            specs.append(
                (
                    self.markets[coin],
                    intent.is_buy,
                    intent.px,
                    intent.sz,
                    cloid,
                    intent.reduce_only,
                    intent.tif,
                )
            )
            pending.append((intent.side, intent))
        if not specs:
            return None
        self._fee_banner(coin)
        payload = self.signer.signed_orders(specs)
        try:
            resp = self._post(payload)
        except RateLimited:
            log.warning("place 429 %s", coin)
            raise
        if is_alo_reject(resp):
            if plan.take:
                log.warning("IOC flatten missed %s %s", coin, resp)
                return {"iocFail": True, "resp": resp}
            log.warning("ALO px rejected, will requote %s %s", coin, resp)
            self.last_plan_key[coin] = ""
            return {"aloReject": True, "resp": resp}
        if not action_ok(resp):
            if plan.take:
                log.warning("IOC flatten failed %s %s", coin, resp)
                return {"iocFail": True, "resp": resp}
            if is_alo_reject(resp):
                log.warning("ALO px rejected, will requote %s", coin)
                self.last_plan_key[coin] = ""
                return {"aloReject": True, "resp": resp}
            log.warning("order not ok %s %s", coin, resp)
            return {"resp": resp}
        statuses = resp_statuses(resp)
        now = time.time()
        accepted = 0
        for idx, (_side, intent) in enumerate(pending):
            st = statuses[idx] if idx < len(statuses) else None
            err = status_error(st)
            if err:
                if is_alo_reject(err):
                    log.warning("ALO px rejected, will requote %s %s", coin, err)
                    self.last_plan_key[coin] = ""
                    self._diag_quotes(coin, accepted, take=plan.take)
                    return {"aloReject": True, "resp": resp}
                if plan.take:
                    log.warning("IOC flatten status %s %s", coin, err)
                    return {"iocFail": True, "resp": resp}
                log.warning("order status %s %s", coin, err)
                continue
            oid = status_oid(st)
            slot = RestSlot(
                oid=oid,
                cloid=specs[idx][4],
                px=intent.px,
                sz=intent.sz,
                tif=intent.tif,
                reduce_only=intent.reduce_only,
                placed_at=now,
            )
            self.rests[coin][intent.side] = slot
            accepted += 1
        log.info(
            "%s %s %s",
            "take" if plan.take else "rest",
            coin,
            [(i.side, i.px, i.sz, i.tif, "ro" if i.reduce_only else "") for i in plan.intents],
        )
        self._diag_quotes(coin, accepted, take=plan.take)
        return {"resp": resp}

    def requote(self, coin: str, top: BookTop) -> None:
        self.refresh_positions()
        plan = self.plan_for(coin, top)
        key = plan.key()
        stale = (
            plan.mode == "flat"
            and self.quote_placed_at.get(coin, 0) > 0
            and (time.time() - self.quote_placed_at[coin]) * 1000 > STALE_QUOTE_MS
        )
        have_buy = side_occupied(self.rests, coin, "B")
        have_sell = side_occupied(self.rests, coin, "A")
        want_buy = any(i.is_buy for i in plan.intents)
        want_sell = any(not i.is_buy for i in plan.intents)
        same_coverage = have_buy == want_buy and have_sell == want_sell
        if plan.take:
            now = time.time()
            if now - self.last_take_at.get(coin, 0.0) < IOC_GAP_S:
                self.maybe_deadman()
                return
            log.info("flatten take %s age=%.1fs szi=%s", coin, plan.age_ms / 1000, plan.szi)
            try:
                self.cancel_coin_rests(refetch=True)
            except Exception as exc:  # noqa: BLE001
                log.warning("cancel before IOC %s: %s", coin, exc)
            if side_occupied(self.rests, coin, "B") or side_occupied(self.rests, coin, "A"):
                log.warning("still have rests after cancel; skip IOC place %s", coin)
                self.maybe_deadman()
                return
            self.last_take_at[coin] = now
            try:
                self._place(coin, plan)
            except RateLimited:
                log.warning("IOC 429 %s; continue", coin)
            except Exception as exc:  # noqa: BLE001
                if is_alo_reject(exc):
                    log.warning("ALO/post-only reject on take path %s: %s", coin, exc)
                else:
                    log.warning("IOC flatten retry %s: %s", coin, exc)
            self.last_plan_key[coin] = ""
            self.quote_placed_at[coin] = 0.0
            self.refresh_positions(force=True)
            self.maybe_deadman()
            return
        if not plan.intents:
            log.warning("no maker plan %s; skip", coin)
            self.last_plan_key[coin] = ""
            self.maybe_deadman()
            return
        if same_coverage and key == self.last_plan_key.get(coin) and not stale:
            extras = list(self.extra_oids.get(coin, []))
            if extras:
                try:
                    self._cancel_pairs(coin, extras, [])
                    self.extra_oids[coin] = []
                except Exception as exc:  # noqa: BLE001
                    log.warning("sweep extras %s: %s", coin, exc)
            self.maybe_deadman()
            return
        if stale:
            log.info("stale same-price rest >45s, cancel+replace %s", coin)
        else:
            log.info("plan change, cancel then place %s stage=%s tif=%s", coin, plan.stage, plan.tif)
        try:
            self.cancel_coin_rests(refetch=True)
        except Exception as exc:  # noqa: BLE001
            if is_alo_reject(exc):
                log.warning("ALO reject during cancel/replace %s: %s", coin, exc)
                self.last_plan_key[coin] = ""
                return
            log.warning("cancel before replace %s: %s", coin, exc)
        try:
            placed = self._place(coin, plan)
        except RateLimited:
            log.warning("place 429 %s; continue loop", coin)
            return
        except Exception as exc:  # noqa: BLE001
            if is_alo_reject(exc):
                log.warning("ALO px rejected, will requote %s: %s", coin, exc)
                self.last_plan_key[coin] = ""
                return
            log.warning("place failed %s: %s", coin, exc)
            return
        if placed and placed.get("aloReject"):
            return
        self.last_plan_key[coin] = key
        self.quote_placed_at[coin] = time.time() if plan.mode == "flat" else 0.0
        self.maybe_deadman()

    def step(self, feed: BookFeed | None) -> None:
        self._feed = feed
        try:
            self.refresh_positions()
        except Exception as exc:  # noqa: BLE001
            log.warning("position poll: %s", exc)
        try:
            self.poll_user_fills()
        except Exception as exc:  # noqa: BLE001
            log.warning("fill-diag poll: %s", exc)
        self.flush_markouts()
        for coin in self.markets:
            try:
                top = self.book_for(coin, feed)
                if top is None or top.bid is None or top.ask is None:
                    continue
                self.requote(coin, top)
            except Exception as exc:  # noqa: BLE001
                if is_alo_reject(exc):
                    log.warning("ALO px rejected, will requote %s: %s", coin, exc)
                    self.last_plan_key[coin] = ""
                    continue
                log.warning("live step %s: %s", coin, exc)
        self.maybe_deadman()

    def shutdown_cancels(self) -> None:
        limited = False
        try:
            result = self.cancel_coin_rests(refetch=True)
            limited = bool(result.get("rateLimited"))
        except Exception as exc:  # noqa: BLE001
            log.warning("shutdown cancel: %s", exc)
            limited = True
        leftover = [
            (self.markets[coin].asset_id, slot.cloid)
            for coin, sides in self.rests.items()
            for slot in sides.values()
            if slot.cloid and is_bot_cloid(slot.cloid)
        ]
        if leftover:
            try:
                resp = self._post(self.signer.signed_cancel_cloids(leftover))
                log.info("shutdown cloid cancel %s", resp)
            except Exception as exc:  # noqa: BLE001
                log.warning("shutdown cloid cancel: %s", exc)
        if limited:
            try:
                self.schedule_deadman(DEADMAN_FALLBACK_MS)
            except Exception as exc:  # noqa: BLE001
                log.warning("shutdown dead-man fallback: %s", exc)
        else:
            self.clear_deadman()


def _log_agent(client: InfoClient, signer: LiveSigner) -> None:
    if not signer.is_agent:
        return
    extras = client.extra_agents(signer.account, optional=True)
    if extras is None:
        log.info("extraAgents unavailable; continuing with agent %s", signer.signer_address)
        return
    found = any(
        str(row.get("address") or row.get("agentAddress") or row.get("agent") or "").lower()
        == signer.signer_address.lower()
        for row in extras
        if isinstance(row, dict)
    )
    if found:
        log.info("agent %s is approved on master %s", signer.signer_address, signer.account)
    else:
        log.warning(
            "agent %s not listed in extraAgents for %s; orders may fail until approved on-site",
            signer.signer_address,
            signer.account,
        )


def run_live(settings: Settings, *, seconds: float | None = None) -> int:
    require_live(settings)
    signer = LiveSigner(settings.private_key or "", settings.account)
    client = InfoClient(settings.api_url)
    feed: BookFeed | None = None
    quoter: LiveQuoter | None = None
    try:
        _meta, markets, _ctxs = load_io_markets(client, settings.coins)
        dex = perp_dex_info(client.perp_dexs(), ALLOWED_DEX)
        log.info(
            "LIVE isolated MM  dex=%s/%s  master=%s  signer=%s  agent=%s  "
            "notional=$%s  coins=%s  builder=%s fee=0",
            dex.get("name"),
            dex.get("fullName"),
            signer.account,
            signer.signer_address,
            signer.is_agent,
            live_notional(settings),
            ",".join(settings.coins),
            "0xcD254d2A328f7f67C7c6FEf930A4757516F7b601",
        )
        log.info("%s", describe_fee_model(settings))
        log.info("%s", ALO_TIF_NOTE)
        log.info(
            "dead-man is account-wide: scheduleCancel +20s while running; "
            "clear (no time) on clean stop. Official min 5s; 429 fallback +6s."
        )
        log.info(
            "fill-diag observe-only: FILL_DIAG jsonl per fill (+3s markout) and "
            "quote/fill counters split io:ANTH vs io:SNDK. SNDK session=rth|ah "
            "(Mon–Fri 09:30–16:00 America/New_York; no holiday calendar). "
            "AH markout stays in its own bucket."
        )
        _log_agent(client, signer)
        user_state = client.clearinghouse_state(signer.account, ALLOWED_DEX, optional=True)
        if user_state:
            log.info("isolated user state withdrawable=%s", user_state.get("withdrawable"))
        quoter = LiveQuoter(settings, markets, client, signer)
        if user_state:
            parsed = positions_from_state(user_state, tuple(markets), markets)
            if parsed is not None:
                quoter.pos = parsed
        quoter.bootstrap_isolated()

        stop = {"flag": False}

        def _handle(_signum: int, _frame: object) -> None:
            stop["flag"] = True

        signal.signal(signal.SIGINT, _handle)
        signal.signal(signal.SIGTERM, _handle)

        quoter.poll_user_fills(force=True)
        feed = BookFeed(
            settings.ws_url,
            settings.coins,
            quoter.on_book,
            user=signer.account,
            on_user_fills=quoter.on_user_fills,
        )
        quoter._feed = feed
        feed.start()
        if not feed.wait_ready():
            log.error("websocket did not become ready")
            feed.stop()
            return 1
        deadline = time.time() + seconds if seconds else None
        while not stop["flag"]:
            if deadline and time.time() >= deadline:
                break
            quoter.step(feed)
            time.sleep(LOOP_S)
        return 0
    finally:
        if feed is not None:
            feed.stop()
        if quoter is not None:
            try:
                quoter.flush_markouts()
            except Exception as exc:  # noqa: BLE001
                log.warning("fill-diag shutdown flush: %s", exc)
            try:
                quoter.shutdown_cancels()
            except Exception as exc:  # noqa: BLE001
                log.warning("shutdown: %s", exc)
        client.close()
