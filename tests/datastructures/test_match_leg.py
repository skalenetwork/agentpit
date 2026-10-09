"""The user-leg truth table: what did THIS account do on this row.

A NORMAL match yields two legs — buyer and seller, same token, one price.
A MINT/MERGE yields two legs on DIFFERENT tokens whose prices sum to MICRO.
This is pure: no database, no I/O.
"""

from __future__ import annotations

from agentpit.datastructures.match_leg import MICRO, legs_for_user


def _row(**over):
    row = {
        "TAKER_API_KEY": "taker",
        "MAKER_API_KEY": "maker",
        "ASSET_ID": "yes",
        "MAKER_ASSET_ID": "no",
        "MATCH_KIND": "NORMAL",
        "SIDE": "BUY",
        "PRICE": 300_000,
        "TRADE_SIZE": 100,
    }
    row.update(over)
    return row


def test_a_normal_match_gives_the_taker_its_side_and_the_maker_the_opposite():
    row = _row(MAKER_ASSET_ID="yes")
    taker = legs_for_user(row, "taker")
    maker = legs_for_user(row, "maker")
    assert [(l.token_id, l.side, l.price_micro) for l in taker] == [
        ("yes", "BUY", 300_000)
    ]
    assert [(l.token_id, l.side, l.price_micro) for l in maker] == [
        ("yes", "SELL", 300_000)
    ]


def test_a_mint_gives_both_sides_a_buy_of_different_tokens():
    row = _row(MATCH_KIND="MINT")
    taker = legs_for_user(row, "taker")[0]
    maker = legs_for_user(row, "maker")[0]
    assert (taker.token_id, taker.side) == ("yes", "BUY")
    assert (maker.token_id, maker.side) == ("no", "BUY")
    # The pair costs exactly $1.
    assert taker.price_micro + maker.price_micro == MICRO
    assert maker.price_micro == 300_000  # the stored price IS the maker's


def test_a_merge_gives_both_sides_a_sell_of_different_tokens():
    row = _row(MATCH_KIND="MERGE", SIDE="SELL")
    taker = legs_for_user(row, "taker")[0]
    maker = legs_for_user(row, "maker")[0]
    assert (taker.token_id, taker.side) == ("yes", "SELL")
    assert (maker.token_id, maker.side) == ("no", "SELL")
    assert taker.price_micro + maker.price_micro == MICRO


def test_a_null_match_kind_takes_the_normal_path():
    """Fixtures insert into `trades` directly and leave the columns NULL."""
    row = _row(MATCH_KIND=None, MAKER_ASSET_ID=None)
    maker = legs_for_user(row, "maker")[0]
    assert (maker.token_id, maker.side, maker.price_micro) == ("yes", "SELL", 300_000)


def test_a_self_matched_row_yields_both_legs():
    """The row is inserted directly: the matcher now skips same-family makers,
    but self-trades from before that guard are still in history. Both legs are
    real."""
    row = _row(TAKER_API_KEY="same", MAKER_API_KEY="same", MAKER_ASSET_ID="yes")
    legs = legs_for_user(row, "same")
    assert sorted(l.side for l in legs) == ["BUY", "SELL"]
    assert {l.token_id for l in legs} == {"yes"}


def test_a_stranger_owns_no_legs():
    assert legs_for_user(_row(), "nobody") == []
