from __future__ import annotations

import pytest

from entropy_bot.coins import (
    CoinError,
    FOREIGN_EXAMPLES,
    assert_tradable,
    contains_foreign_venue,
    hip3_asset_id,
    perp_dex_index,
    resolve_markets,
    validate_coin_list,
)
from tests.conftest import IO_META, PERP_DEXS


def test_io_is_index_10():
    assert perp_dex_index(PERP_DEXS, "io") == 10


def test_asset_ids_from_mocked_meta():
    markets = resolve_markets(PERP_DEXS, IO_META)
    assert markets["io:ANTH"].asset_id == hip3_asset_id(10, 1) == 200_001
    assert markets["io:SNDK"].asset_id == hip3_asset_id(10, 2) == 200_002
    assert markets["io:ANTH"].index_in_meta == 1
    assert markets["io:SNDK"].index_in_meta == 2
    assert markets["io:ANTH"].sz_decimals == 3
    assert markets["io:SNDK"].sz_decimals == 4


def test_case_sensitive_names():
    with pytest.raises(CoinError):
        assert_tradable("io:anth")
    with pytest.raises(CoinError):
        assert_tradable("IO:ANTH")
    with pytest.raises(CoinError):
        assert_tradable("io:sndk")
    assert assert_tradable("io:ANTH") == "io:ANTH"
    assert assert_tradable("io:SNDK") == "io:SNDK"


def test_refuse_delisted_and_foreign():
    for name in ("io:OAI", "io:IONQ", *FOREIGN_EXAMPLES, "xyz:xyz100"):
        with pytest.raises(CoinError):
            assert_tradable(name)


def test_validate_coin_list_rejects_xyz():
    with pytest.raises(CoinError, match="foreign venue"):
        validate_coin_list(["io:ANTH", "xyz:SNDK"])


def test_resolve_never_includes_foreign_or_delisted():
    markets = resolve_markets(PERP_DEXS, IO_META)
    assert set(markets) == {"io:ANTH", "io:SNDK"}
    assert "xyz:SNDK" not in markets
    assert "io:OAI" not in markets


def test_contains_foreign_venue_detects_xyz():
    assert contains_foreign_venue({"coin": "xyz:SNDK"}) == ["xyz:SNDK"]
    assert contains_foreign_venue({"coin": "io:ANTH"}) == []
