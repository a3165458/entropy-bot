"""HIP-3 coin resolution for EntropyIO (`io`) only.

Asset IDs follow the official Hyperliquid formula:

    100000 + perp_dex_index * 10000 + index_in_meta

`perp_dex_index` is the index of the DEX object in the `perpDexs` array
(looked up every run; `io` is currently 10). `index_in_meta` is the
index of the coin in `meta` with `{"type":"meta","dex":"io"}`.

Coin names are case-sensitive. Foreign venues (`xyz:`, `vntl:`) and
delisted `io` listings are refused.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence

from entropy_bot.errors import CoinError

ALLOWED_DEX = "io"
ALLOWED_COINS: tuple[str, ...] = ("io:ANTH", "io:SNDK")
FORBIDDEN_VENUES: tuple[str, ...] = ("xyz", "vntl")
DELISTED_COINS: tuple[str, ...] = ("io:OAI", "io:IONQ")
FOREIGN_EXAMPLES: tuple[str, ...] = ("xyz:SNDK", "vntl:ANTHROPIC")

HIP3_BASE = 100_000
HIP3_DEX_STRIDE = 10_000
DEFAULT_DEX = "io"


@dataclass(frozen=True)
class Market:
    coin: str
    asset_id: int
    index_in_meta: int
    perp_dex_index: int
    sz_decimals: int
    max_leverage: int
    only_isolated: bool
    margin_mode: str
    growth_mode: bool
    deployer_fee_scale: float
    is_delisted: bool
    collateral_token: int


def parse_coin(name: str) -> tuple[str, str]:
    if not isinstance(name, str) or ":" not in name:
        raise CoinError(f"HIP-3 coins must be DEX-prefixed, got {name!r}")
    dex, symbol = name.split(":", 1)
    if not dex or not symbol:
        raise CoinError(f"invalid HIP-3 coin {name!r}")
    return dex, symbol


def assert_tradable(name: str) -> str:
    """Accept only live EntropyIO coins. Case-sensitive; never xyz/vntl."""
    if name != name.strip():
        raise CoinError(f"coin name is case-sensitive and must not be padded: {name!r}")
    dex, _symbol = parse_coin(name)
    if dex in FORBIDDEN_VENUES:
        raise CoinError(
            f"refusing foreign venue {name!r}; this bot only trades EntropyIO "
            f"({', '.join(ALLOWED_COINS)})"
        )
    if dex != ALLOWED_DEX:
        raise CoinError(f"refusing dex {dex!r}; expected {ALLOWED_DEX!r}")
    if name in DELISTED_COINS:
        raise CoinError(f"refusing delisted coin {name!r}")
    if name not in ALLOWED_COINS:
        raise CoinError(
            f"unsupported coin {name!r}; allowed: {', '.join(ALLOWED_COINS)}"
        )
    return name


def validate_coin_list(coins: Iterable[str]) -> tuple[str, ...]:
    resolved = tuple(assert_tradable(c) for c in coins)
    if not resolved:
        raise CoinError("COINS must list at least one EntropyIO market")
    seen: set[str] = set()
    unique: list[str] = []
    for coin in resolved:
        if coin not in seen:
            unique.append(coin)
            seen.add(coin)
    return tuple(unique)


def contains_foreign_venue(payload: Any) -> list[str]:
    """Walk a payload and return any xyz:/vntl: coin strings found."""
    found: list[str] = []

    def walk(value: Any) -> None:
        if isinstance(value, str):
            if value.startswith(tuple(f"{v}:" for v in FORBIDDEN_VENUES)):
                found.append(value)
            return
        if isinstance(value, Mapping):
            for k, v in value.items():
                walk(k)
                walk(v)
            return
        if isinstance(value, (list, tuple, set)):
            for item in value:
                walk(item)

    walk(payload)
    return found


def assert_no_foreign_venue(payload: Any) -> None:
    hits = contains_foreign_venue(payload)
    if hits:
        raise CoinError(f"payload referenced foreign venue coins: {hits}")


def perp_dex_index(perp_dexs: Sequence[Any], dex_name: str = DEFAULT_DEX) -> int:
    for idx, entry in enumerate(perp_dexs):
        if isinstance(entry, Mapping) and entry.get("name") == dex_name:
            return idx
    raise CoinError(f"DEX {dex_name!r} not found in perpDexs")


def perp_dex_info(perp_dexs: Sequence[Any], dex_name: str = DEFAULT_DEX) -> dict[str, Any]:
    for entry in perp_dexs:
        if isinstance(entry, Mapping) and entry.get("name") == dex_name:
            return dict(entry)
    raise CoinError(f"DEX {dex_name!r} not found in perpDexs")


def index_in_meta(universe: Sequence[Mapping[str, Any]], coin: str) -> int:
    for idx, asset in enumerate(universe):
        if asset.get("name") == coin:
            return idx
    raise CoinError(f"{coin} not found in io meta universe")


def hip3_asset_id(dex_index: int, meta_index: int) -> int:
    return HIP3_BASE + dex_index * HIP3_DEX_STRIDE + meta_index


def _as_float(value: Any, default: float = 0.0) -> float:
    if value is None:
        return default
    return float(value)


def _growth_enabled(asset: Mapping[str, Any]) -> bool:
    mode = asset.get("growthMode")
    if isinstance(mode, bool):
        return mode
    if isinstance(mode, str):
        return mode.lower() in {"enabled", "true", "on", "1"}
    return False


def resolve_markets(
    perp_dexs: Sequence[Any],
    meta: Mapping[str, Any],
    coins: Sequence[str] | None = None,
) -> dict[str, Market]:
    wanted = validate_coin_list(coins or ALLOWED_COINS)
    dex_index = perp_dex_index(perp_dexs, ALLOWED_DEX)
    universe = meta.get("universe") or []
    collateral = int(meta.get("collateralToken") or 0)
    markets: dict[str, Market] = {}
    for coin in wanted:
        meta_index = index_in_meta(universe, coin)
        asset = universe[meta_index]
        if asset.get("isDelisted"):
            raise CoinError(f"refusing delisted coin {coin}")
        markets[coin] = Market(
            coin=coin,
            asset_id=hip3_asset_id(dex_index, meta_index),
            index_in_meta=meta_index,
            perp_dex_index=dex_index,
            sz_decimals=int(asset["szDecimals"]),
            max_leverage=int(asset.get("maxLeverage") or 1),
            only_isolated=bool(asset.get("onlyIsolated", True)),
            margin_mode=str(asset.get("marginMode") or "strictIsolated"),
            growth_mode=_growth_enabled(asset),
            deployer_fee_scale=_as_float(asset.get("deployerFeeScale"), 1.0),
            is_delisted=bool(asset.get("isDelisted", False)),
            collateral_token=collateral,
        )
    return markets
