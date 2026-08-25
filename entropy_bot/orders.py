"""Signed L1 order / cancel payloads. Default TIF is ALO (post-only)."""

from __future__ import annotations

import time
from typing import Any, Literal

from eth_account import Account
from eth_account.signers.local import LocalAccount
from hyperliquid.utils.signing import (
    get_timestamp_ms,
    order_request_to_order_wire,
    order_wires_to_order_action,
    sign_l1_action,
)
from hyperliquid.utils.types import Cloid

from entropy_bot.coins import Market, assert_no_foreign_venue, assert_tradable
from entropy_bot.errors import LiveGuardError
from entropy_bot.precision import isolated_leverage

DEFAULT_TIF = "Alo"
BOT_CLOID_PREFIX = "0x45424f54"  # "EBOT"
Side = Literal["B", "A"]  # bid / ask


def wallet_from_key(private_key: str) -> LocalAccount:
    key = private_key.strip()
    if not key.startswith("0x"):
        key = "0x" + key
    return Account.from_key(key)


def account_address(private_key: str, configured: str | None = None) -> str:
    derived = wallet_from_key(private_key).address
    if configured:
        return configured
    return derived


def make_cloid(coin: str, side: Side, seq: int) -> str:
    assert_tradable(coin)
    tag = coin.split(":", 1)[1].encode("ascii")[:4].ljust(4, b"\0").hex()
    side_byte = "00" if side == "B" else "01"
    # 16-byte cloid: EBOT(4) + coin(4) + side(1) + seq(7)
    seq_hex = f"{seq & 0xFFFFFFFFFFFFFF:014x}"
    raw = f"{BOT_CLOID_PREFIX}{tag}{side_byte}{seq_hex}"
    return Cloid.from_str(raw).to_raw()


def is_bot_cloid(cloid: str | None) -> bool:
    if not cloid:
        return False
    return cloid.lower().startswith(BOT_CLOID_PREFIX.lower())


def alo_order_wire(
    *,
    asset_id: int,
    is_buy: bool,
    limit_px: float,
    sz: float,
    cloid: str,
    reduce_only: bool = False,
) -> dict[str, Any]:
    if asset_id < 100_000:
        raise ValueError("HIP-3 asset ids start at 100000; refusing validator-perp id")
    order = {
        "coin": "",  # unused once asset_id is supplied
        "is_buy": is_buy,
        "sz": sz,
        "limit_px": limit_px,
        "order_type": {"limit": {"tif": DEFAULT_TIF}},
        "reduce_only": reduce_only,
        "cloid": Cloid.from_str(cloid),
    }
    wire = dict(order_request_to_order_wire(order, asset_id))
    if wire["t"] != {"limit": {"tif": "Alo"}}:
        raise ValueError("TIF must be ALO; refusing IOC/GTC default")
    assert_no_foreign_venue(wire)
    return wire


def alo_order_action(wires: list[dict[str, Any]]) -> dict[str, Any]:
    action = order_wires_to_order_action(wires, builder=None, grouping="na")
    assert_no_foreign_venue(action)
    for order in action["orders"]:
        tif = order.get("t", {}).get("limit", {}).get("tif")
        if tif != "Alo":
            raise ValueError(f"refusing non-ALO order tif={tif!r}")
    return action


def cancel_by_cloid_action(cancels: list[dict[str, Any]]) -> dict[str, Any]:
    action = {"type": "cancelByCloid", "cancels": cancels}
    assert_no_foreign_venue(action)
    return action


def cancel_oids_action(cancels: list[dict[str, Any]]) -> dict[str, Any]:
    action = {"type": "cancel", "cancels": cancels}
    assert_no_foreign_venue(action)
    return action


def update_leverage_action(market: Market, leverage: int) -> dict[str, Any]:
    lev = isolated_leverage(leverage, market.max_leverage)
    return {
        "type": "updateLeverage",
        "asset": market.asset_id,
        "isCross": False,
        "leverage": lev,
    }


def sign_and_wrap(wallet: LocalAccount, action: dict[str, Any], *, is_mainnet: bool = True) -> dict[str, Any]:
    assert_no_foreign_venue(action)
    nonce = get_timestamp_ms()
    signature = sign_l1_action(wallet, action, None, nonce, None, is_mainnet)
    return {
        "action": action,
        "nonce": nonce,
        "signature": signature,
    }


class LiveSigner:
    """Signs and (optionally) posts official /exchange actions."""

    def __init__(self, private_key: str, account: str | None = None) -> None:
        if not private_key:
            raise LiveGuardError("signing refused: HYPERLIQUID_PRIVATE_KEY is not set")
        self.wallet = wallet_from_key(private_key)
        self.account = account or self.wallet.address
        self._seq = int(time.time() * 1000) & 0xFFFFFFFFFFFFFF

    def next_cloid(self, coin: str, side: Side) -> str:
        self._seq += 1
        return make_cloid(coin, side, self._seq)

    def signed_alo_orders(
        self,
        specs: list[tuple[Market, bool, float, float, str]],
    ) -> dict[str, Any]:
        wires = [
            alo_order_wire(
                asset_id=market.asset_id,
                is_buy=is_buy,
                limit_px=px,
                sz=sz,
                cloid=cloid,
            )
            for market, is_buy, px, sz, cloid in specs
        ]
        return sign_and_wrap(self.wallet, alo_order_action(wires))

    def signed_cancel_cloids(self, pairs: list[tuple[int, str]]) -> dict[str, Any]:
        cancels = [{"asset": asset, "cloid": cloid} for asset, cloid in pairs]
        return sign_and_wrap(self.wallet, cancel_by_cloid_action(cancels))

    def signed_update_leverage(self, market: Market, leverage: int) -> dict[str, Any]:
        return sign_and_wrap(self.wallet, update_leverage_action(market, leverage))


def format_fee_banner(
    coin: str,
    fee_line: str,
    notional: float,
    maker_usd: float,
    rebate_usd: float | None = None,
) -> str:
    extra = ""
    if rebate_usd is not None:
        extra = f" → ALO rebate credit ${rebate_usd:.6f}"
    return (
        f"FEE before live ALO order {coin}: {fee_line} | "
        f"quote ${notional:.2f} → est maker ${maker_usd:.6f}{extra}"
    )
