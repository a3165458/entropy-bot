from __future__ import annotations

import json

import pytest

from entropy_bot.coins import CoinError, contains_foreign_venue
from entropy_bot.errors import LiveGuardError
from entropy_bot.orders import (
    DEFAULT_TIF,
    LiveSigner,
    alo_order_action,
    alo_order_wire,
    make_cloid,
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


def test_resolve_uses_mocked_meta_not_hardcoded_xyz():
    markets = resolve_markets(PERP_DEXS, IO_META)
    dumped = json.dumps({k: v.asset_id for k, v in markets.items()})
    assert "xyz:SNDK" not in dumped
    assert "vntl:ANTHROPIC" not in dumped
