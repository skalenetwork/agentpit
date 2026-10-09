"""A trade row must name BOTH tokens that moved.

One ASSET_ID is enough for a NORMAL match, where both parties transact in the
same token. A MINT gives the maker the market's OTHER outcome and a MERGE
burns it, and nothing recorded that — so an account that mints could not have
its holdings reconstructed from its own trade history.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

import pytest

from tests.db_helpers import fresh_test_conn


@pytest.fixture()
def db() -> Any:
    conn = fresh_test_conn()
    yield conn
    conn.close()


def test_the_trades_table_carries_both_tokens(db):
    """The columns exist and accept the two new values."""
    db.execute(
        "INSERT INTO trades (TRADE_ID, ASSET_ID, MAKER_ASSET_ID, MATCH_KIND, "
        "SIDE, PRICE, TRADE_SIZE, STATUS) "
        "VALUES (%s, 'tok-a', 'tok-b', 'MINT', 'BUY', 400000, 100, 'matched')",
        (uuid.uuid4().hex,),
    )
    row = db.execute(
        "SELECT ASSET_ID, MAKER_ASSET_ID, MATCH_KIND FROM trades "
        "WHERE MATCH_KIND = 'MINT' LIMIT 1"
    ).fetchone()
    assert row["ASSET_ID"] == "tok-a"
    assert row["MAKER_ASSET_ID"] == "tok-b"
    assert row["MATCH_KIND"] == "MINT"


def test_the_columns_are_nullable_for_rows_written_before_they_existed(db):
    db.execute(
        "INSERT INTO trades (TRADE_ID, ASSET_ID, SIDE, PRICE, TRADE_SIZE, STATUS) "
        "VALUES (%s, 'tok-a', 'BUY', 400000, 100, 'matched')",
        (uuid.uuid4().hex,),
    )
    row = db.execute(
        "SELECT MAKER_ASSET_ID, MATCH_KIND FROM trades WHERE ASSET_ID='tok-a' "
        "AND MATCH_KIND IS NULL LIMIT 1"
    ).fetchone()
    assert row is not None
    assert row["MAKER_ASSET_ID"] is None


# ----- backfilling rows written before the columns existed --------------------

from agentpit.datastructures.condition_id import ConditionId  # noqa: E402
from agentpit.datastructures.create_market_request import (  # noqa: E402
    CreateMarketRequest,
)
from agentpit.datastructures.market_state import MarketState  # noqa: E402
from agentpit.db.table_write import TableWrite  # noqa: E402
from scripts.backfill_trade_match_kind import backfill_trade_match_kind  # noqa: E402


def _hex32(seed: str) -> str:
    return "0x" + seed.encode().hex().ljust(64, "0")[:64]


def _binary_market(db, seed: str):
    """A two-outcome market whose tokens are `<seed>-y` and `<seed>-n`."""
    return TableWrite.create_market(
        db,
        CreateMarketRequest(
            question=f"{seed}?",
            description="d",
            erc1155_tokens=[(f"{seed}-y", "Yes"), (f"{seed}-n", "No")],
            slug=seed,
            condition_id=ConditionId(_hex32(seed)),
            state=MarketState.ACTIVE,
        ),
        is_polygon_market=False,
    )


def _legacy_trade(db, *, market, asset, taker_side, maker_side):
    """A row in the pre-column shape: no MAKER_ASSET_ID, no MATCH_KIND."""
    db.execute(
        "INSERT INTO trades (TRADE_ID, MARKET, ASSET_ID, SIDE, PRICE, "
        "TRADE_SIZE, STATUS, MAKER_ORDERS) "
        "VALUES (%s, %s, %s, %s, 400000, 100, 'matched', %s)",
        (
            uuid.uuid4().hex,
            market.condition_id.value,
            asset,
            taker_side,
            json.dumps([{"side": maker_side, "asset_id": asset}]),
        ),
    )


def _kinds(db):
    rows = db.execute(
        "SELECT ASSET_ID, MAKER_ASSET_ID, MATCH_KIND FROM trades "
        "WHERE MATCH_KIND IS NOT NULL ORDER BY ASSET_ID"
    ).fetchall()
    return [(r["ASSET_ID"], r["MAKER_ASSET_ID"], r["MATCH_KIND"]) for r in rows]


def test_backfill_labels_a_normal_match_and_keeps_one_token(db):
    m = _binary_market(db, "bfn")
    _legacy_trade(db, market=m, asset="bfn-y", taker_side="BUY", maker_side="SELL")
    backfill_trade_match_kind(db)
    assert _kinds(db) == [("bfn-y", "bfn-y", "NORMAL")]


def test_backfill_gives_a_mint_maker_the_complementary_token(db):
    """Both sides buying is a mint: the maker receives the OTHER outcome."""
    m = _binary_market(db, "bfm")
    _legacy_trade(db, market=m, asset="bfm-y", taker_side="BUY", maker_side="BUY")
    backfill_trade_match_kind(db)
    assert _kinds(db) == [("bfm-y", "bfm-n", "MINT")]


def test_backfill_gives_a_merge_maker_the_complementary_token(db):
    """Both sides selling is a merge: the maker burns the OTHER outcome."""
    m = _binary_market(db, "bfg")
    _legacy_trade(db, market=m, asset="bfg-n", taker_side="SELL", maker_side="SELL")
    backfill_trade_match_kind(db)
    assert _kinds(db) == [("bfg-n", "bfg-y", "MERGE")]


def test_backfill_leaves_maker_asset_null_when_the_complement_cant_be_resolved(db):
    """A token no market claims can't be resolved to a complement. Leaving
    MAKER_ASSET_ID NULL keeps `_token_flow`'s COALESCE(MAKER_ASSET_ID,
    ASSET_ID) reading the taker's own token, instead of asserting a token
    the maker never actually held."""
    db.execute(
        "INSERT INTO trades (TRADE_ID, ASSET_ID, SIDE, PRICE, TRADE_SIZE, "
        "STATUS, MAKER_ORDERS) VALUES (%s, 'orphan-tok', 'BUY', 400000, 100, "
        "'matched', %s)",
        (uuid.uuid4().hex, json.dumps([{"side": "BUY"}])),
    )
    backfill_trade_match_kind(db)
    row = db.execute(
        "SELECT MAKER_ASSET_ID, MATCH_KIND FROM trades WHERE ASSET_ID='orphan-tok'"
    ).fetchone()
    assert row["MATCH_KIND"] == "MINT"
    assert row["MAKER_ASSET_ID"] is None


def test_backfill_leaves_already_labelled_rows_alone(db):
    """Idempotent: it only fills rows the columns never reached."""
    m = _binary_market(db, "bfi")
    db.execute(
        "INSERT INTO trades (TRADE_ID, MARKET, ASSET_ID, MAKER_ASSET_ID, "
        "MATCH_KIND, SIDE, PRICE, TRADE_SIZE, STATUS, MAKER_ORDERS) "
        "VALUES (%s, %s, 'bfi-y', 'DELIBERATE', 'NORMAL', 'BUY', 1, 1, "
        "'matched', %s)",
        (uuid.uuid4().hex, m.condition_id.value,
         json.dumps([{"side": "BUY"}])),
    )
    backfill_trade_match_kind(db)
    # Were it re-derived, the BUY/BUY pair would relabel this MINT.
    assert _kinds(db) == [("bfi-y", "DELIBERATE", "NORMAL")]


def test_backfill_is_a_no_op_on_a_second_run(db):
    m = _binary_market(db, "bft")
    _legacy_trade(db, market=m, asset="bft-y", taker_side="BUY", maker_side="BUY")
    backfill_trade_match_kind(db)
    first = _kinds(db)
    backfill_trade_match_kind(db)
    assert _kinds(db) == first


def test_the_unlabelled_index_exists_with_the_match_kind_predicate(db):
    """idx_trades_unlabelled is what keeps the backfill's "is there anything
    to do" probe an index scan instead of a full sequential scan once every
    row is labelled — assert it actually exists with that predicate, not
    just that the probe returns the right rows."""
    row = db.execute(
        "SELECT indexdef FROM pg_indexes "
        "WHERE tablename = 'trades' AND indexname = 'idx_trades_unlabelled'"
    ).fetchone()
    assert row is not None
    assert "match_kind" in row["indexdef"].lower()


# ----- flows across every match kind -----------------------------------------

from agentpit.services.account_service import AccountService  # noqa: E402


def _trade(db, *, market, asset, maker_asset, kind, taker_side, price,
           size, taker="taker-key", maker="maker-key"):
    db.execute(
        "INSERT INTO trades (TRADE_ID, MARKET, ASSET_ID, MAKER_ASSET_ID, "
        "MATCH_KIND, SIDE, PRICE, TRADE_SIZE, STATUS, TAKER_API_KEY, "
        "MAKER_API_KEY) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,'matched',%s,%s)",
        (uuid.uuid4().hex, market.condition_id.value, asset, maker_asset,
         kind, taker_side, price, size, taker, maker),
    )


def test_a_mint_maker_acquires_the_complement_at_the_stored_price(db):
    """The bug in one test: the maker of a mint receives the OTHER token, and
    before this its holdings had no row at all."""
    m = _binary_market(db, "fm1")
    _trade(db, market=m, asset="fm1-y", maker_asset="fm1-n", kind="MINT",
           taker_side="BUY", price=300_000, size=100)
    flow = AccountService._token_flow(db, "maker-key", "fm1-n")
    assert flow.bought_size == 100
    assert flow.avg_buy_price_micro == 300_000
    assert flow.sold_size == 0
    # And nothing landed on the taker's token for this user.
    assert AccountService._token_flow(db, "maker-key", "fm1-y").bought_size == 0


def test_a_mint_taker_pays_the_complement_of_the_stored_price(db):
    m = _binary_market(db, "fm2")
    _trade(db, market=m, asset="fm2-y", maker_asset="fm2-n", kind="MINT",
           taker_side="BUY", price=300_000, size=100)
    flow = AccountService._token_flow(db, "taker-key", "fm2-y")
    assert flow.bought_size == 100
    # The pair costs $1: the maker put up 0.30, so the taker put up 0.70.
    assert flow.avg_buy_price_micro == 700_000


def test_a_merge_disposes_on_both_sides(db):
    m = _binary_market(db, "fg1")
    _trade(db, market=m, asset="fg1-y", maker_asset="fg1-n", kind="MERGE",
           taker_side="SELL", price=400_000, size=100)
    taker = AccountService._token_flow(db, "taker-key", "fg1-y")
    maker = AccountService._token_flow(db, "maker-key", "fg1-n")
    assert taker.sold_size == 100 and maker.sold_size == 100
    assert taker.bought_size == 0 and maker.bought_size == 0
    # Proceeds sum to the $1 the merge returns.
    assert taker.sold_proceeds + maker.sold_proceeds == 1_000_000 * 100


def test_a_normal_match_still_moves_one_token_in_two_directions(db):
    m = _binary_market(db, "fn1")
    _trade(db, market=m, asset="fn1-y", maker_asset="fn1-y", kind="NORMAL",
           taker_side="BUY", price=250_000, size=100)
    assert AccountService._token_flow(db, "taker-key", "fn1-y").bought_size == 100
    assert AccountService._token_flow(db, "maker-key", "fn1-y").sold_size == 100


def test_net_size_can_no_longer_go_negative_on_a_mint_heavy_account(db):
    """The production symptom: trade nets reading -55,142 shares, which is
    impossible for real holdings and meant the maker's leg was landing on the
    wrong token."""
    m = _binary_market(db, "fn2")
    for _ in range(3):
        _trade(db, market=m, asset="fn2-y", maker_asset="fn2-n", kind="MINT",
               taker_side="BUY", price=300_000, size=100)
    for key, tok in (("taker-key", "fn2-y"), ("maker-key", "fn2-n")):
        assert AccountService._token_flow(db, key, tok).net_size == 300
    for key, tok in (("taker-key", "fn2-n"), ("maker-key", "fn2-y")):
        assert AccountService._token_flow(db, key, tok).net_size == 0


def test_a_self_matched_normal_sell_cannot_manufacture_a_negative_net(db):
    """The matcher has no same-account guard: a resting order of this user's
    can be crossed by a later order of theirs, same api_key on both sides of
    the row. Collapsing that to a single `is_taker` used to book only the
    taker's SELL leg — sold_size with no offsetting buy — which is how a net
    could go negative even though the account transacted with itself for net
    zero."""
    m = _binary_market(db, "fs1")
    _trade(db, market=m, asset="fs1-y", maker_asset="fs1-y", kind="NORMAL",
           taker_side="SELL", price=400_000, size=100,
           taker="self-key", maker="self-key")
    flow = AccountService._token_flow(db, "self-key", "fs1-y")
    assert flow.bought_size == 100
    assert flow.sold_size == 100
    assert flow.net_size == 0


def test_a_self_matched_mint_still_costs_one_dollar_for_the_pair(db):
    """One account standing on both sides of a mint acquires BOTH outcomes,
    and the taker/maker prices still sum to $1 — the pair's economics don't
    change just because one account happens to be both parties."""
    m = _binary_market(db, "fs2")
    _trade(db, market=m, asset="fs2-y", maker_asset="fs2-n", kind="MINT",
           taker_side="BUY", price=300_000, size=100,
           taker="self-key", maker="self-key")
    yes = AccountService._token_flow(db, "self-key", "fs2-y")
    no = AccountService._token_flow(db, "self-key", "fs2-n")
    assert yes.bought_size == 100
    assert no.bought_size == 100
    assert yes.avg_buy_price_micro + no.avg_buy_price_micro == 1_000_000
