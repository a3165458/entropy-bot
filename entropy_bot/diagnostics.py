"""Observe-only fill-rate and +3s markout diagnostics for live MM.

Logging only. Does not choose prices, sizes, cancels, or dead-man timing.

Holidays: no NYSE calendar. A weekday 09:30–16:00 America/New_York is `rth`
even if the cash session is closed. Everything else (nights, weekends) is `ah`.
AH markout is labeled and kept in its own SNDK bucket; it is never folded into
an RTH average.
"""

from __future__ import annotations

import json
import logging
import threading
import time as time_mod
from dataclasses import dataclass, field
from datetime import datetime, time, timezone
from typing import Any, Callable
from zoneinfo import ZoneInfo

from entropy_bot.coins import ALLOWED_COINS
from entropy_bot.precision import spread_bps as spread_bps_vs_mid
from entropy_bot.quoting import BookTop

log = logging.getLogger("entropy_bot.diagnostics")

ANTH = "io:ANTH"
SNDK = "io:SNDK"
DIAG_COINS = ALLOWED_COINS
MARKOUT_WAIT_S = 3.0
BOOK_STALE_S = 15.0
NY_TZ = ZoneInfo("America/New_York")
RTH_START = time(9, 30)
RTH_END = time(16, 0)

PeekBook = Callable[[str], "BookSnap | None"]


@dataclass(frozen=True)
class BookSnap:
    mid: float | None
    spread: float | None
    spread_bps: float | None
    age_s: float

    @property
    def stale(self) -> bool:
        return self.age_s > BOOK_STALE_S


@dataclass
class PendingMarkout:
    coin: str
    side: str
    fill_px: float
    mid_at_fill: float | None
    spread: float | None
    spread_bps: float | None
    session: str | None
    due_at: float
    fill_key: str


def markout_bps(side: str, fill_px: float, mid_3s: float) -> float:
    """Signed +3s markout in bps vs fill price.

    Buy that mid-rises is positive; sell that mid-falls is positive.
    """
    if fill_px <= 0:
        raise ValueError("fill_px must be positive")
    side_n = normalize_side(side)
    if side_n == "buy":
        return (mid_3s - fill_px) / fill_px * 10_000.0
    if side_n == "sell":
        return (fill_px - mid_3s) / fill_px * 10_000.0
    raise ValueError(f"side must be buy/sell, got {side!r}")


def fill_rate(fills: int, quotes: int) -> float | None:
    if quotes <= 0:
        return None
    return fills / quotes


def sndk_session(when: datetime | None = None) -> str:
    """`rth` = Mon–Fri 09:30–16:00 America/New_York; else `ah`. No holiday calendar."""
    dt = datetime.now(NY_TZ) if when is None else when
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=NY_TZ)
    else:
        dt = dt.astimezone(NY_TZ)
    if dt.weekday() >= 5:
        return "ah"
    clock = dt.timetz().replace(tzinfo=None)
    if RTH_START <= clock < RTH_END:
        return "rth"
    return "ah"


def session_for(coin: str, when: datetime | None = None) -> str | None:
    if coin == SNDK:
        return sndk_session(when)
    return None


def normalize_side(raw: Any) -> str | None:
    if raw is None:
        return None
    text = str(raw).strip().lower()
    if text in {"b", "buy", "bid"}:
        return "buy"
    if text in {"a", "s", "sell", "ask"}:
        return "sell"
    return None


def normalize_diag_coin(raw: Any) -> str | None:
    if raw is None:
        return None
    text = str(raw).strip()
    if text in DIAG_COINS:
        return text
    for coin in DIAG_COINS:
        if text == coin.split(":", 1)[1]:
            return coin
    return None


def when_from_fill(raw: dict[str, Any], when: datetime | None = None) -> datetime | None:
    if when is not None:
        return when
    stamp = raw.get("time")
    if stamp is None:
        return None
    try:
        value = float(stamp)
    except (TypeError, ValueError):
        return None
    if value > 1e12:
        value /= 1000.0
    return datetime.fromtimestamp(value, tz=timezone.utc)


def fill_key(row: dict[str, Any]) -> str:
    tid = row.get("tid")
    if tid is not None:
        return f"tid:{tid}"
    parts = (
        row.get("time"),
        row.get("oid"),
        row.get("px"),
        row.get("sz"),
        row.get("side"),
        row.get("coin"),
        row.get("hash"),
    )
    return "row:" + "|".join("" if p is None else str(p) for p in parts)


def snap_from_top(top: BookTop | None, age_s: float) -> BookSnap:
    if top is None or top.bid is None or top.ask is None or top.bid <= 0 or top.ask <= 0:
        return BookSnap(mid=None, spread=None, spread_bps=None, age_s=age_s)
    mid = (top.bid + top.ask) / 2.0
    spread = top.ask - top.bid
    return BookSnap(
        mid=mid,
        spread=spread,
        spread_bps=spread_bps_vs_mid(top.bid, top.ask),
        age_s=age_s,
    )


def _fresh_fields(snap: BookSnap | None) -> tuple[float | None, float | None, float | None]:
    if snap is None or snap.stale:
        return None, None, None
    return snap.mid, snap.spread, snap.spread_bps


@dataclass
class FillDiagnostics:
    coins: tuple[str, ...] = DIAG_COINS
    quotes: dict[tuple[str, str | None], int] = field(default_factory=dict)
    fills: dict[tuple[str, str | None], int] = field(default_factory=dict)
    markouts: dict[tuple[str, str | None], list[float]] = field(default_factory=dict)
    seen: set[str] = field(default_factory=set)
    pending: list[PendingMarkout] = field(default_factory=list)
    rows: list[dict[str, Any]] = field(default_factory=list)
    primed: bool = False
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def __post_init__(self) -> None:
        for coin in self.coins:
            if coin == SNDK:
                for session in ("rth", "ah"):
                    key = (coin, session)
                    self.quotes.setdefault(key, 0)
                    self.fills.setdefault(key, 0)
                    self.markouts.setdefault(key, [])
            else:
                key = (coin, None)
                self.quotes.setdefault(key, 0)
                self.fills.setdefault(key, 0)
                self.markouts.setdefault(key, [])

    def bucket(self, coin: str, session: str | None = None) -> tuple[str, str | None]:
        if coin == SNDK:
            return (coin, session or "ah")
        return (coin, None)

    def snapshot(self, coin: str, session: str | None = None) -> dict[str, Any]:
        key = self.bucket(coin, session)
        quotes = self.quotes.get(key, 0)
        fills = self.fills.get(key, 0)
        marks = list(self.markouts.get(key, []))
        row: dict[str, Any] = {
            "type": "counters",
            "coin": coin,
            "quotes": quotes,
            "fills": fills,
            "fill_rate": fill_rate(fills, quotes),
        }
        if coin == SNDK:
            row["session"] = key[1]
        if marks:
            row["markout_n"] = len(marks)
            row["markout_avg_bps"] = sum(marks) / len(marks)
        return row

    def note_quotes(self, coin: str, n: int, *, when: datetime | None = None) -> dict[str, Any] | None:
        coin_n = normalize_diag_coin(coin)
        if coin_n is None or n <= 0:
            return None
        session = session_for(coin_n, when)
        with self._lock:
            key = self.bucket(coin_n, session)
            self.quotes[key] = self.quotes.get(key, 0) + int(n)
            row = self.snapshot(coin_n, session)
        self._emit(row)
        return row

    def ingest_fills(
        self,
        raw_fills: list[dict[str, Any]] | None,
        peek_book: PeekBook,
        *,
        prime: bool = False,
        now: float | None = None,
        when: datetime | None = None,
    ) -> list[PendingMarkout]:
        if not raw_fills:
            if prime:
                self.primed = True
            return []
        created: list[PendingMarkout] = []
        for raw in raw_fills:
            if not isinstance(raw, dict):
                continue
            pending = self._ingest_one(raw, peek_book, prime=prime, now=now, when=when)
            if pending is not None:
                created.append(pending)
        if prime:
            self.primed = True
        return created

    def flush_markouts(self, peek_book: PeekBook, *, now: float | None = None) -> list[dict[str, Any]]:
        ts = time_mod.time() if now is None else now
        due: list[PendingMarkout] = []
        with self._lock:
            keep: list[PendingMarkout] = []
            for item in self.pending:
                if item.due_at <= ts:
                    due.append(item)
                else:
                    keep.append(item)
            self.pending = keep
        emitted: list[dict[str, Any]] = []
        for item in due:
            row = self._finish_markout(item, peek_book)
            emitted.append(row)
        return emitted

    def _ingest_one(
        self,
        raw: dict[str, Any],
        peek_book: PeekBook,
        *,
        prime: bool,
        now: float | None,
        when: datetime | None,
    ) -> PendingMarkout | None:
        key = fill_key(raw)
        coin = normalize_diag_coin(raw.get("coin"))
        side = normalize_side(raw.get("side"))
        try:
            fill_px = float(raw.get("px"))
        except (TypeError, ValueError):
            fill_px = 0.0
        if coin is None or side is None or fill_px <= 0:
            return None
        with self._lock:
            if key in self.seen:
                return None
            self.seen.add(key)
            if prime:
                return None
            session = session_for(coin, when_from_fill(raw, when))
            bkey = self.bucket(coin, session)
            self.fills[bkey] = self.fills.get(bkey, 0) + 1
        snap = None
        try:
            snap = peek_book(coin)
        except Exception as exc:  # noqa: BLE001
            log.warning("fill-diag peek book at fill %s: %s", coin, exc)
        mid, spread, spr_bps = _fresh_fields(snap)
        ts = time_mod.time() if now is None else now
        pending = PendingMarkout(
            coin=coin,
            side=side,
            fill_px=fill_px,
            mid_at_fill=mid,
            spread=spread,
            spread_bps=spr_bps,
            session=session,
            due_at=ts + MARKOUT_WAIT_S,
            fill_key=key,
        )
        with self._lock:
            self.pending.append(pending)
        return pending

    def _finish_markout(self, item: PendingMarkout, peek_book: PeekBook) -> dict[str, Any]:
        mid_3s: float | None = None
        mark: float | None = None
        try:
            snap = peek_book(item.coin)
        except Exception as exc:  # noqa: BLE001
            log.warning("fill-diag peek book +3s %s: %s", item.coin, exc)
            snap = None
        if snap is not None and not snap.stale and snap.mid is not None:
            mid_3s = snap.mid
            try:
                mark = markout_bps(item.side, item.fill_px, mid_3s)
            except ValueError:
                mark = None
        if mark is not None:
            with self._lock:
                bucket = self.markouts.setdefault(self.bucket(item.coin, item.session), [])
                bucket.append(mark)
        with self._lock:
            counters = self.snapshot(item.coin, item.session)
        row: dict[str, Any] = {
            "type": "fill",
            "coin": item.coin,
            "side": item.side,
            "fill_px": item.fill_px,
            "mid_at_fill": item.mid_at_fill,
            "mid_3s": mid_3s,
            "markout_bps": mark,
            "spread": item.spread,
            "spread_bps": item.spread_bps,
            "quotes": counters["quotes"],
            "fills": counters["fills"],
            "fill_rate": counters["fill_rate"],
        }
        if item.session is not None:
            row["session"] = item.session
        if "markout_avg_bps" in counters:
            row["markout_avg_bps"] = counters["markout_avg_bps"]
        self._emit(row)
        return row

    def _emit(self, row: dict[str, Any]) -> None:
        self.rows.append(row)
        line = json.dumps(row, separators=(",", ":"), ensure_ascii=False)
        print(f"FILL_DIAG {line}", flush=True)
        log.info("FILL_DIAG %s", line)
