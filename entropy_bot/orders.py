"""Signed L1 order / cancel payloads.

Two-sided maker quotes and reduce-only flatten both stay ALO. IOC is refused.
Builder is always Entropy (fee 0) so the rebate path matches the userscript.

Agent vs master (official SDK, not a guess):
- `HYPERLIQUID_PRIVATE_KEY` signs. That key may be the master *or* an approved API/agent.
- `sign_l1_action(..., vaultAddress=None)` is correct for both. `vaultAddress` is only
  for vault/subaccount trading and is hashed into the connectionId.
- `HYPERLIQUID_ACCOUNT` is the master used for info queries (positions, open orders).
  Official `Exchange(wallet=agent, account_address=master, vault_address=None)`.
"""

from __future__ import annotations

import logging
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
from entropy_bot.errors import LiveGuardError, error_text, is_weight_limit_error

log = logging.getLogger("entropy_bot.orders")

DEFAULT_TIF = "Alo"
ALLOWED_TIFS = frozenset({"Alo"})
BOT_CLOID_PREFIX = "0x45424f54"  # "EBOT"
ENTROPY_BUILDER = "0xcD254d2A328f7f67C7c6FEf930A4757516F7b601"
ENTROPY_BUILDER_FEE = 0  # tenths of a basis point
ENTROPY_BUILDER_INFO = {"b": ENTROPY_BUILDER.lower(), "f": ENTROPY_BUILDER_FEE}
Side = Literal["B", "A"]  # bid / ask


def wallet_from_key(private_key: str) -> LocalAccount:
    key = private_key.strip()
    if not key.startswith("0x"):
        key = "0x" + key
    return Account.from_key(key)


def account_address(private_key: str, configured: str | None = None) -> str:
    """Master address for queries. Never log `private_key`."""
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


def as_oid(value: Any) -> int:
    """HIP-3 oids exceed int32. Never truncate with `| 0` / 32-bit int."""
    if value is None or isinstance(value, bool):
        raise ValueError(f"illegal oid: {value!r}")
    if isinstance(value, str):
        text = value.strip()
        if text.startswith("-") or not text.isdigit():
            raise ValueError(f"illegal oid: {value!r}")
        n = int(text)
    else:
        n = int(value)
        if n != value:
            raise ValueError(f"illegal oid: {value!r}")
    if n < 0:
        raise ValueError(f"illegal oid: {value!r}")
    return n


def _normalize_tif(tif: str) -> str:
    raw = str(tif or DEFAULT_TIF)
    if raw.lower() == "ioc":
        raise ValueError("IOC refused; flatten/exit is maker ALO only")
    return "Alo"


def order_wire(
    *,
    asset_id: int,
    is_buy: bool,
    limit_px: float,
    sz: float,
    cloid: str | None = None,
    reduce_only: bool = False,
    tif: str = DEFAULT_TIF,
) -> dict[str, Any]:
    if asset_id < 100_000:
        raise ValueError("HIP-3 asset ids start at 100000; refusing validator-perp id")
    tif_n = _normalize_tif(tif)
    if tif_n not in ALLOWED_TIFS:
        raise ValueError(f"unsupported tif={tif!r}")
    if tif_n != "Alo":
        raise ValueError("non-ALO TIF refused; flatten/exit is maker ALO only")
    order: dict[str, Any] = {
        "coin": "",  # unused once asset_id is supplied
        "is_buy": is_buy,
        "sz": sz,
        "limit_px": limit_px,
        "order_type": {"limit": {"tif": tif_n}},
        "reduce_only": reduce_only,
    }
    if cloid:
        order["cloid"] = Cloid.from_str(cloid)
    wire = dict(order_request_to_order_wire(order, asset_id))
    assert_no_foreign_venue(wire)
    return wire


def alo_order_wire(
    *,
    asset_id: int,
    is_buy: bool,
    limit_px: float,
    sz: float,
    cloid: str,
    reduce_only: bool = False,
) -> dict[str, Any]:
    wire = order_wire(
        asset_id=asset_id,
        is_buy=is_buy,
        limit_px=limit_px,
        sz=sz,
        cloid=cloid,
        reduce_only=reduce_only,
        tif=DEFAULT_TIF,
    )
    if wire["t"] != {"limit": {"tif": "Alo"}}:
        raise ValueError("TIF must be ALO; refusing IOC/GTC default")
    return wire


def entropy_builder() -> dict[str, Any]:
    return dict(ENTROPY_BUILDER_INFO)


def order_action(wires: list[dict[str, Any]], *, builder: dict[str, Any] | None = None) -> dict[str, Any]:
    action = order_wires_to_order_action(wires, builder=builder or entropy_builder(), grouping="na")
    assert_no_foreign_venue(action)
    for order in action["orders"]:
        tif = order.get("t", {}).get("limit", {}).get("tif")
        reduce_only = bool(order.get("r"))
        if tif == "Alo":
            continue
        raise ValueError(f"refusing non-ALO tif={tif!r} reduce_only={reduce_only}")
    return action


def alo_order_action(wires: list[dict[str, Any]]) -> dict[str, Any]:
    for wire in wires:
        tif = wire.get("t", {}).get("limit", {}).get("tif")
        if tif != "Alo":
            raise ValueError(f"refusing non-ALO order tif={tif!r}")
    return order_action(wires)


def cancel_by_cloid_action(cancels: list[dict[str, Any]]) -> dict[str, Any]:
    action = {"type": "cancelByCloid", "cancels": cancels}
    assert_no_foreign_venue(action)
    return action


def cancel_oids_action(cancels: list[dict[str, Any]]) -> dict[str, Any]:
    action = {"type": "cancel", "cancels": cancels}
    assert_no_foreign_venue(action)
    return action


def schedule_cancel_action(time_ms: int | None) -> dict[str, Any]:
    action: dict[str, Any] = {"type": "scheduleCancel"}
    if time_ms is not None:
        action["time"] = int(time_ms)
    return action


def update_leverage_action(market: Market, leverage: int) -> dict[str, Any]:
    from entropy_bot.precision import isolated_leverage

    lev = isolated_leverage(leverage, market.max_leverage)
    return {
        "type": "updateLeverage",
        "asset": market.asset_id,
        "isCross": False,
        "leverage": lev,
    }


def sign_and_wrap(
    wallet: LocalAccount,
    action: dict[str, Any],
    *,
    is_mainnet: bool = True,
    vault_address: str | None = None,
) -> dict[str, Any]:
    """Sign an L1 action. `vault_address` is vault/subaccount only — never the master for an agent."""
    assert_no_foreign_venue(action)
    nonce = get_timestamp_ms()
    signature = sign_l1_action(wallet, action, vault_address, nonce, None, is_mainnet)
    payload: dict[str, Any] = {
        "action": action,
        "nonce": nonce,
        "signature": signature,
    }
    if vault_address:
        payload["vaultAddress"] = vault_address
    return payload


class LiveSigner:
    """Signs official /exchange actions with an agent or master key.

    Never logs the private key. `account` is the master that holds positions.
    """

    def __init__(
        self,
        private_key: str,
        account: str | None = None,
        *,
        vault_address: str | None = None,
    ) -> None:
        if not private_key:
            raise LiveGuardError("signing refused: HYPERLIQUID_PRIVATE_KEY is not set")
        self.wallet = wallet_from_key(private_key)
        self.signer_address = self.wallet.address
        self.account = account or self.signer_address
        self.vault_address = vault_address
        self.is_agent = self.account.lower() != self.signer_address.lower()
        self._seq = int(time.time() * 1000) & 0xFFFFFFFFFFFFFF
        if self.is_agent:
            log.info(
                "agent signing: key=%s  master=%s  vaultAddress=None (official API-wallet path)",
                self.signer_address,
                self.account,
            )
        else:
            log.info("signing as master %s (set HYPERLIQUID_ACCOUNT if this key is an agent)", self.account)

    def next_cloid(self, coin: str, side: Side) -> str:
        self._seq += 1
        return make_cloid(coin, side, self._seq)

    def wrap(self, action: dict[str, Any]) -> dict[str, Any]:
        return sign_and_wrap(self.wallet, action, vault_address=self.vault_address)

    def signed_orders(
        self,
        specs: list[tuple[Market, bool, float, float, str | None, bool, str]],
    ) -> dict[str, Any]:
        wires = [
            order_wire(
                asset_id=market.asset_id,
                is_buy=is_buy,
                limit_px=px,
                sz=sz,
                cloid=cloid,
                reduce_only=reduce_only,
                tif=tif,
            )
            for market, is_buy, px, sz, cloid, reduce_only, tif in specs
        ]
        return self.wrap(order_action(wires))

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
        return self.wrap(alo_order_action(wires))

    def signed_cancel_cloids(self, pairs: list[tuple[int, str]]) -> dict[str, Any]:
        cancels = [{"asset": asset, "cloid": cloid} for asset, cloid in pairs]
        return self.wrap(cancel_by_cloid_action(cancels))

    def signed_cancel_oids(self, pairs: list[tuple[int, int]]) -> dict[str, Any]:
        cancels = [{"a": int(asset), "o": as_oid(oid)} for asset, oid in pairs]
        return self.wrap(cancel_oids_action(cancels))

    def signed_schedule_cancel(self, time_ms: int | None) -> dict[str, Any]:
        return self.wrap(schedule_cancel_action(time_ms))

    def signed_update_leverage(self, market: Market, leverage: int) -> dict[str, Any]:
        return self.wrap(update_leverage_action(market, leverage))


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


def resp_statuses(resp: Any) -> list[Any]:
    try:
        statuses = resp["response"]["data"]["statuses"]
    except (TypeError, KeyError):
        return []
    return statuses if isinstance(statuses, list) else []


def status_oid(st: Any) -> int | None:
    if st is None or isinstance(st, str):
        return None
    if not isinstance(st, dict):
        return None
    for key in ("resting", "filled"):
        inner = st.get(key)
        if isinstance(inner, dict) and inner.get("oid") is not None:
            return as_oid(inner["oid"])
    if st.get("oid") is not None:
        return as_oid(st["oid"])
    return None


def status_error(st: Any) -> str:
    if st is None:
        return ""
    if isinstance(st, str):
        return st
    if isinstance(st, dict) and isinstance(st.get("error"), str):
        return st["error"]
    return ""


def blob_text(value: Any) -> str:
    return error_text(value)


def is_alo_reject(err: Any) -> bool:
    text = blob_text(err)
    return bool(
        text
        and (
            "Bad Alo Px" in text
            or "alo px" in text.lower()
            or "post-only" in text.lower()
            or "would match immediately" in text.lower()
        )
    )


def is_rate_limited(err: Any) -> bool:
    text = blob_text(err)
    return "429" in text or "rate limit" in text.lower() or is_weight_limit_error(text)


def action_ok(resp: Any) -> bool:
    if not resp:
        return False
    if isinstance(resp, dict) and resp.get("status") not in (None, "ok"):
        return False
    return True
