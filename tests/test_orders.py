from __future__ import annotations

import json

import pytest

from entropy_bot.coins import CoinError, contains_foreign_venue
from entropy_bot.errors import LiveGuardError
from entropy_bot.orders import (
    DEFAULT_TIF,
    ENTROPY_BUILDER,
    LiveSigner,
    alo_order_action,
    alo_order_wire,
    as_oid,
    make_cloid,
    order_action,
    order_wire,
    sign_and_wrap,
    update_leverage_action,
)
from entropy_bot.quoting import book_top, desired_quotes
from tests.conftest import IO_META, PERP_DEXS
from entropy_bot.coins import resolve_markets


def test_default_tif_is_alo():
    assert DEFAULT_TIF == "Alo"


def test_alo_payload_uses_mocked_asset_id(markets):
    anth = markets["io:ANTH"]
    cloid = make_cloid("io:ANTH", "B", 1)
    wire = alo_order_wire(
        asset_id=anth.asset_id,
        is_buy=True,
        limit_px=1979.2,
        sz=0.025,
        cloid=cloid,
    )
    assert wire["a"] == 200_001
    assert wire["b"] is True
    assert wire["t"] == {"limit": {"tif": "Alo"}}
    assert "Ioc" not in json.dumps(wire)
    assert "Gtc" not in json.dumps(wire)
    action = alo_order_action([wire])
    assert action["type"] == "order"
    assert action["orders"][0]["a"] == 200_001
    assert contains_foreign_venue(action) == []


def test_sndk_asset_id_in_payload(markets):
    sndk = markets["io:SNDK"]
    wire = alo_order_wire(
        asset_id=sndk.asset_id,
        is_buy=False,
        limit_px=1504.6,
        sz=0.0333,
        cloid=make_cloid("io:SNDK", "A", 2),
    )
    assert wire["a"] == 200_002
    assert wire["t"]["limit"]["tif"] == "Alo"


def test_isolated_leverage_is_not_cross(markets):
    action = update_leverage_action(markets["io:ANTH"], 2)
    assert action["isCross"] is False
    assert action["asset"] == 200_001
    assert action["leverage"] == 2
    # cap below market max (3)
    action = update_leverage_action(markets["io:ANTH"], 99)
    assert action["leverage"] == 3


def test_make_cloid_rejects_xyz():
    with pytest.raises(CoinError):
        make_cloid("xyz:SNDK", "B", 1)


def test_quotes_never_emit_xyz(markets):
    top = book_top(
        "io:SNDK",
        {
            "levels": [
                [{"px": "1504.3", "sz": "1", "n": 1}],
                [{"px": "1504.4", "sz": "1", "n": 1}],
            ]
        },
    )
    intents = desired_quotes(markets["io:SNDK"], top, notional_usd=50, offset_ticks=2)
    blob = json.dumps([intent.__dict__ for intent in intents])
    assert "xyz:SNDK" not in blob
    assert "vntl:" not in blob
    assert all(i.coin == "io:SNDK" for i in intents)
    assert all(i.asset_id == 200_002 for i in intents)


def test_live_signer_refuses_empty_key():
    with pytest.raises(LiveGuardError):
        LiveSigner("")


def test_order_action_includes_entropy_builder(markets):
    anth = markets["io:ANTH"]
    wire = alo_order_wire(
        asset_id=anth.asset_id,
        is_buy=True,
        limit_px=1979.2,
        sz=0.025,
        cloid=make_cloid("io:ANTH", "B", 1),
    )
    action = alo_order_action([wire])
    assert action["builder"] == {"b": ENTROPY_BUILDER.lower(), "f": 0}


def test_ioc_refused_even_for_reduce_only_flatten(markets):
    anth = markets["io:ANTH"]
    with pytest.raises(ValueError, match="IOC refused"):
        order_wire(
            asset_id=anth.asset_id,
            is_buy=False,
            limit_px=1985.0,
            sz=0.02,
            reduce_only=True,
            tif="Ioc",
        )
    with pytest.raises(ValueError, match="IOC refused"):
        order_wire(
            asset_id=anth.asset_id,
            is_buy=True,
            limit_px=1985.0,
            sz=0.02,
            reduce_only=False,
            tif="Ioc",
        )


def test_oid_not_truncated_to_int32():
    big = 5_000_000_000  # > uint32; `| 0` / int32 would wrap
    assert as_oid(big) == big
    assert as_oid(str(big)) == big
    assert as_oid(big) != (big & 0xFFFFFFFF)
    with pytest.raises(ValueError):
        as_oid(-1)


def test_agent_signing_does_not_put_master_in_vault(markets):
    agent_key = "0x" + "ab" * 32
    master = "0x" + "11" * 20
    signer = LiveSigner(agent_key, master)
    assert signer.is_agent is True
    assert signer.account.lower() == master.lower()
    assert signer.wallet.address.lower() != master.lower()
    payload = signer.signed_update_leverage(markets["io:ANTH"], 2)
    assert "vaultAddress" not in payload
    from hyperliquid.utils.signing import recover_agent_or_user_from_l1_action

    recovered = recover_agent_or_user_from_l1_action(
        payload["action"],
        payload["signature"],
        None,
        payload["nonce"],
        None,
        True,
    )
    assert recovered.lower() == signer.signer_address.lower()
    wrapped = sign_and_wrap(signer.wallet, payload["action"], vault_address=None)
    assert "vaultAddress" not in wrapped


def test_resolve_uses_mocked_meta_not_hardcoded_xyz():
    markets = resolve_markets(PERP_DEXS, IO_META)
    dumped = json.dumps({k: v.asset_id for k, v in markets.items()})
    assert "xyz:SNDK" not in dumped
    assert "vntl:ANTHROPIC" not in dumped
