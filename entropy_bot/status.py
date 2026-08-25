from __future__ import annotations

from typing import Any

from entropy_bot.coins import (
    ALLOWED_DEX,
    DELISTED_COINS,
    Market,
    perp_dex_info,
    resolve_markets,
)
from entropy_bot.config import Settings
from entropy_bot.fees import ALO_TIF_NOTE, describe_fee_model, estimate_from_settings, estimate_market_fees
from entropy_bot.precision import spread_bps
from entropy_bot.quoting import book_top
from entropy_bot.rest import InfoClient


def load_io_markets(client: InfoClient, coins: tuple[str, ...]) -> tuple[dict[str, Any], dict[str, Market], list[Any]]:
    dexs = client.perp_dexs()
    meta_ctx = client.meta_and_asset_ctxs(ALLOWED_DEX)
    meta, ctxs = meta_ctx[0], meta_ctx[1]
    markets = resolve_markets(dexs, meta, coins)
    return meta, markets, ctxs


def _ctx_for(markets: dict[str, Market], ctxs: list[Any], coin: str) -> dict[str, Any]:
    return dict(ctxs[markets[coin].index_in_meta])


def isolated_positions(state: dict[str, Any], coins: tuple[str, ...]) -> list[str]:
    lines: list[str] = []
    for item in state.get("assetPositions") or []:
        pos = item.get("position") or {}
        coin = pos.get("coin")
        if coin not in coins:
            continue
        lev = pos.get("leverage") or {}
        lines.append(
            f"  isolated {coin}: szi={pos.get('szi')} px={pos.get('entryPx')} "
            f"lev={lev.get('type')}/{lev.get('value')} margin={pos.get('marginUsed')} "
            f"uPnL={pos.get('unrealizedPnl')}"
        )
    return lines


def format_status(
    dex_info: dict[str, Any],
    markets: dict[str, Market],
    ctxs: list[Any],
    books: dict[str, Any],
    user_state: dict[str, Any] | None = None,
    settings: Settings | None = None,
) -> str:
    lines = [
        f"DEX {dex_info.get('name')} ({dex_info.get('fullName')})  "
        f"perp_dex_index={next(iter(markets.values())).perp_dex_index}  "
        f"collateralToken={next(iter(markets.values())).collateral_token} (USDC)",
        f"coins: {', '.join(markets)}  isolated-only",
        f"ignored delisted: {', '.join(DELISTED_COINS)}",
        f"{ALO_TIF_NOTE}",
    ]
    if settings is not None:
        lines.append(describe_fee_model(settings))
    lines.append("")
    for coin in markets:
        m = markets[coin]
        ctx = _ctx_for(markets, ctxs, coin)
        top = book_top(coin, books.get(coin) or {"levels": [[], []]})
        bid, ask = top.bid, top.ask
        spr = spread_bps(bid, ask) if bid and ask else None
        fee = estimate_from_settings(m, settings) if settings is not None else estimate_market_fees(m)
        lines.extend(
            [
                f"{coin}  asset={m.asset_id}  szDecimals={m.sz_decimals}  "
                f"maxLev={m.max_leverage}  {m.margin_mode}  onlyIsolated={m.only_isolated}",
                f"  growthMode={'enabled' if m.growth_mode else 'off'}  "
                f"deployerFeeScale={m.deployer_fee_scale:g}",
                f"  mark={ctx.get('markPx')}  oracle={ctx.get('oraclePx')}  mid={ctx.get('midPx')}",
                f"  funding={ctx.get('funding')}  OI={ctx.get('openInterest')}  "
                f"24h vol={ctx.get('dayNtlVlm')}",
                f"  bid={bid}  ask={ask}  spread="
                + (f"{spr:.2f} bps" if spr is not None else "n/a"),
                f"  {fee.summary()}",
                "",
            ]
        )
    if user_state:
        pos_lines = isolated_positions(user_state, tuple(markets))
        lines.append("user isolated state (io):")
        lines.extend(pos_lines or ["  no isolated positions on ANTH/SNDK"])
    return "\n".join(lines).rstrip() + "\n"


def run_status(settings: Settings) -> int:
    client = InfoClient(settings.api_url)
    try:
        meta, markets, ctxs = load_io_markets(client, settings.coins)
        dex = perp_dex_info(client.perp_dexs(), ALLOWED_DEX)
        books = {coin: client.l2_book(coin) for coin in settings.coins}
        user_state = None
        if settings.account:
            user_state = client.clearinghouse_state(settings.account, ALLOWED_DEX)
        print(format_status(dex, markets, ctxs, books, user_state, settings))
        return 0
    finally:
        client.close()
