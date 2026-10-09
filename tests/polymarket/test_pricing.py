"""compute_market_prices derives per-outcome + scalar prices from the local
book tops and trade tape, in scaled-int ($1.00 == 1_000_000) units."""

from agentpit.datastructures.condition_id import ConditionId
from agentpit.datastructures.market import Market
from agentpit.datastructures.market_state import MarketState
from agentpit.polymarket.pricing import PRICE_ONE, compute_market_prices

# Binary market: outcome[0]=YES token "111", outcome[1]=NO token "222".
_YES, _NO = "111", "222"


def _market() -> Market:
    return Market(
        question="Will it rain?",
        slug="will-it-rain",
        market_id=7,
        condition_id=ConditionId("0x" + "ab" * 32),
        question_id="0x" + "cd" * 32,
        description="desc",
        erc1155_tokens=[(_YES, "Yes"), (_NO, "No")],
        start_date=1_700_000_000,
        end_date=1_800_000_000,
        market_state=MarketState.ACTIVE,
    )


def test_two_sided_yes_book_gives_mid_and_no_complement():
    # YES book 0.14/0.15 -> mid 0.145; NO has no book/trade -> complement.
    p = compute_market_prices(
        _market(), tops={_YES: (140_000, 150_000)}, lasts={}
    )
    assert p.best_bid == 140_000
    assert p.best_ask == 150_000
    assert p.last_trade is None
    assert p.outcome_prices == [145_000, PRICE_ONE - 145_000]  # [0.145, 0.855]


def test_no_book_uses_its_own_mid_not_complement():
    # Both sides have independent books — surface each own mid (arb-visible).
    p = compute_market_prices(
        _market(),
        tops={_YES: (140_000, 160_000), _NO: (300_000, 500_000)},
        lasts={},
    )
    assert p.outcome_prices == [150_000, 400_000]  # YES mid, NO mid (own)


def test_single_resting_side_used_directly():
    p = compute_market_prices(_market(), tops={_YES: (200_000, None)}, lasts={})
    assert p.best_bid == 200_000
    assert p.best_ask is None
    assert p.outcome_prices[0] == 200_000
    assert p.outcome_prices[1] == PRICE_ONE - 200_000


def test_last_trade_fallback_when_no_book():
    p = compute_market_prices(_market(), tops={}, lasts={_YES: 300_000})
    assert p.last_trade == 300_000
    assert p.outcome_prices == [300_000, PRICE_ONE - 300_000]


def test_no_data_falls_back_to_half():
    p = compute_market_prices(_market(), tops={}, lasts={})
    assert p.best_bid is None and p.best_ask is None and p.last_trade is None
    assert p.outcome_prices == [PRICE_ONE // 2, PRICE_ONE // 2]
