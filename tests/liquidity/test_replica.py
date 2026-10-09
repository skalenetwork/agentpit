from agentpit.datastructures.match import Take
from agentpit.liquidity.replica import MICRO, BookReplica, BookSnapshot, to_micro


def _book_msg(asset="A", bids=None, asks=None):
    return {
        "event_type": "book", "asset_id": asset,
        "bids": [{"price": p, "size": s} for p, s in (bids or [])],
        "asks": [{"price": p, "size": s} for p, s in (asks or [])],
    }


def test_to_micro_decimal_strings_never_float():
    assert to_micro("0.48") == 480_000
    assert to_micro(".5") == 500_000          # Polymarket emits ".48"-style strings
    assert to_micro("0.980") == 980_000       # trailing-zero variant
    assert to_micro("145369.13") == 145_369_130_000
    assert to_micro("garbage") is None
    assert to_micro(None) is None


def test_apply_book_replaces_state_and_seeds():
    r = BookReplica("A")
    assert r.snapshot() is None               # unseeded → unusable
    assert r.apply_book(_book_msg(bids=[("0.40", "10")], asks=[("0.60", "5")]))
    assert r.apply_book(_book_msg(bids=[("0.45", "7")], asks=[("0.55", "3")]))
    snap = r.snapshot()
    assert snap.bids == ((450_000, 7_000_000),)   # fully replaced, not merged
    assert snap.asks == ((550_000, 3_000_000),)


def test_apply_book_wrong_asset_rejected():
    r = BookReplica("A")
    assert not r.apply_book(_book_msg(asset="B", bids=[("0.4", "1")]))
    assert r.snapshot() is None


def test_apply_book_skips_off_tick_zero_and_garbage_levels():
    r = BookReplica("A")
    r.apply_book(_book_msg(
        bids=[("0.4005", "10"), ("0.40", "0"), ("x", "1"), ("0.41", "2")],
        asks=[("0.60", "1")]))
    snap = r.snapshot()
    assert snap.bids == ((410_000, 2_000_000),)   # off-tick, zero-size, garbage dropped


def test_snapshot_orders_best_first_regardless_of_input_order():
    # Live feed sends arrays worst-to-best; never trust array order.
    r = BookReplica("A")
    r.apply_book(_book_msg(
        bids=[("0.10", "1"), ("0.40", "2")],     # ascending (worst first)
        asks=[("0.90", "1"), ("0.60", "2")]))    # descending (worst first)
    snap = r.snapshot()
    assert snap.bids[0] == (400_000, 2_000_000)  # best bid first
    assert snap.asks[0] == (600_000, 2_000_000)  # best (lowest) ask first


def test_price_change_replace_semantics_and_delete():
    r = BookReplica("A")
    r.apply_book(_book_msg(bids=[("0.40", "10")], asks=[("0.60", "5")]))
    assert r.apply_price_change_entry(
        {"asset_id": "A", "side": "BUY", "price": "0.40", "size": "3"})
    assert r.snapshot().bids == ((400_000, 3_000_000),)   # replace, not add
    assert r.apply_price_change_entry(
        {"asset_id": "A", "side": "BUY", "price": "0.40", "size": "0"})
    assert r.snapshot().bids == ()                        # size 0 = level removed
    assert r.apply_price_change_entry(
        {"asset_id": "A", "side": "SELL", "price": "0.61", "size": "2"})
    assert r.snapshot().asks == ((600_000, 5_000_000), (610_000, 2_000_000))


def test_price_change_sibling_asset_filtered():
    # price_change messages carry mirrored entries for BOTH sibling asset_ids.
    r = BookReplica("A")
    r.apply_book(_book_msg(bids=[("0.40", "10")], asks=[("0.60", "5")]))
    assert not r.apply_price_change_entry(
        {"asset_id": "SIBLING", "side": "SELL", "price": "0.60", "size": "9"})
    assert r.snapshot().bids == ((400_000, 10_000_000),)


def test_price_change_before_seed_ignored():
    r = BookReplica("A")
    assert not r.apply_price_change_entry(
        {"asset_id": "A", "side": "BUY", "price": "0.40", "size": "3"})


def test_mark_stale_drops_the_book_until_a_fresh_snapshot():
    r = BookReplica("A")
    r.apply_book(_book_msg(bids=[("0.40", "10")], asks=[("0.60", "5")]))
    r.mark_stale()
    assert r.snapshot() is None
    assert not r.apply_price_change_entry(                # deltas dropped while stale
        {"asset_id": "A", "side": "BUY", "price": "0.40", "size": "3"})
    r.apply_book(_book_msg(bids=[("0.30", "1")], asks=[("0.70", "1")]))
    assert r.snapshot().bids == ((300_000, 1_000_000),)   # fresh snapshot re-seeds


def test_crossed_replica_yields_no_snapshot():
    r = BookReplica("A")
    r.apply_book(_book_msg(bids=[("0.60", "1")], asks=[("0.55", "1")]))
    assert r.snapshot() is None


def test_one_sided_and_empty_books_are_valid():
    r = BookReplica("A")
    r.apply_book(_book_msg(bids=[("0.40", "1")], asks=[]))
    snap = r.snapshot()
    assert snap.bids == ((400_000, 1_000_000),) and snap.asks == ()


def test_to_micro_rejects_non_finite_and_overflow():
    assert to_micro("Infinity") is None
    assert to_micro("inf") is None
    assert to_micro("-Infinity") is None
    assert to_micro("NaN") is None
    assert to_micro("1e1000") is None


def test_apply_book_with_infinity_level_does_not_corrupt_other_side():
    r = BookReplica("A")
    r.apply_book(_book_msg(bids=[("0.40", "10")], asks=[("0.60", "5")]))
    r.apply_book(_book_msg(bids=[("0.45", "7")], asks=[("Infinity", "1")]))
    snap = r.snapshot()
    assert snap.asks == ()                       # bad level dropped, not stale-retained
    assert snap.bids == ((450_000, 7_000_000),)  # and no exception escaped


def _seeded(bids=(("0.40", "10"), ("0.39", "20")), asks=(("0.60", "5"), ("0.61", "7"))):
    r = BookReplica("A")
    r.apply_book(_book_msg(bids=bids, asks=asks))
    return r


def _walk(r, agent, side, yes, limit, size):
    return r.take(agent, (side == "BUY") == yes, limit if yes else MICRO - limit, size)


def test_take_walks_the_four_mappings_up_to_the_limit():
    r = _seeded()
    assert _walk(r, "a", "BUY", True, 600_000, 9_000_000) == (
        Take(True, 600_000, 5_000_000, 5_000_000),
    )
    assert _walk(r, "a", "SELL", True, 390_000, 15_000_000) == (
        Take(False, 400_000, 10_000_000, 10_000_000),
        Take(False, 390_000, 20_000_000, 5_000_000),
    )
    assert _walk(r, "a", "BUY", False, 600_000, 3_000_000) == (
        Take(False, 400_000, 10_000_000, 3_000_000),
    )
    assert _walk(r, "a", "SELL", False, 390_000, 20_000_000) == (
        Take(True, 600_000, 5_000_000, 5_000_000),
        Take(True, 610_000, 7_000_000, 7_000_000),
    )
    assert _walk(r, "a", "BUY", True, 590_000, 1_000_000) == ()


def test_take_is_empty_on_an_unseeded_or_crossed_book():
    assert BookReplica("A").take("a", True, 990_000, 1_000_000) == ()
    assert (
        _seeded(bids=(("0.60", "1"),), asks=(("0.55", "1"),)).take(
            "a", True, 990_000, 1
        )
        == ()
    )


def test_use_up_is_per_agent_and_lasts_until_the_size_changes():
    r = _seeded()
    r.use("a", r.take("a", True, 600_000, 3_000_000))
    assert r.take("a", True, 600_000, 9_000_000) == (
        Take(True, 600_000, 5_000_000, 2_000_000),
    )
    assert r.take("b", True, 600_000, 9_000_000) == (
        Take(True, 600_000, 5_000_000, 5_000_000),
    )
    r.apply_book(_book_msg(bids=[("0.40", "10")], asks=[("0.60", "5"), ("0.61", "7")]))
    assert r.take("a", True, 600_000, 9_000_000) == (
        Take(True, 600_000, 5_000_000, 2_000_000),
    )
    r.apply_price_change_entry(
        {"asset_id": "A", "side": "SELL", "price": "0.60", "size": "6"}
    )
    assert r.take("a", True, 600_000, 9_000_000) == (
        Take(True, 600_000, 6_000_000, 6_000_000),
    )


def test_a_yes_buyer_and_a_no_seller_share_the_ask_level():
    r = _seeded()
    r.use("a", _walk(r, "a", "BUY", True, 600_000, 4_000_000))
    assert _walk(r, "a", "SELL", False, 400_000, 9_000_000) == (
        Take(True, 600_000, 5_000_000, 1_000_000),
    )


def test_split_orders_take_at_most_the_level():
    r = _seeded()
    filled = 0
    for _ in range(10):
        takes = r.take("a", True, 600_000, 1_000_000)
        r.use("a", takes)
        filled += sum(t.size for t in takes)
    assert filled == 5_000_000


def test_use_prunes_levels_that_are_gone_or_resized():
    r = _seeded()
    r.use("a", r.take("a", True, 610_000, 12_000_000))
    r.use("b", r.take("b", False, 400_000, 1_000_000))
    r.apply_price_change_entry(
        {"asset_id": "A", "side": "SELL", "price": "0.60", "size": "0"}
    )
    r.apply_price_change_entry(
        {"asset_id": "A", "side": "BUY", "price": "0.40", "size": "11"}
    )
    r.use("b", r.take("b", True, 610_000, 1_000_000))
    assert set(r.used) == {("a", True, 610_000), ("b", True, 610_000)}


def test_flipped_is_the_no_book():
    snap = BookSnapshot(
        "A", bids=((400_000, 1), (390_000, 2)), asks=((600_000, 3), (610_000, 4))
    )
    assert snap.flipped() == BookSnapshot(
        "A", bids=((400_000, 3), (390_000, 4)), asks=((600_000, 1), (610_000, 2))
    )


def test_sizes_are_kept_as_sent():
    r = _seeded(bids=(("0.40", "10.1234567"),), asks=())
    assert r.snapshot().bids == ((400_000, 10_123_457),)
