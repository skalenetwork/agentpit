from typing import Annotated

from fastapi import APIRouter, Body

from agentpit.api.deps import OrderServiceDep
from agentpit.datastructures.book_params import BookParams
from agentpit.datastructures.orderbook_summary import OrderBookSummary

router = APIRouter(tags=["market-data"])

# Every id costs a book read, so one request asks for at most this many.
MAX_BOOKS = 500


@router.get("/book", response_model=OrderBookSummary)
def get_book(token_id: str, service: OrderServiceDep) -> OrderBookSummary:
    return service.get_book(token_id)


@router.post("/books", response_model=list[OrderBookSummary])
def get_books(
    params: Annotated[list[BookParams], Body(max_length=MAX_BOOKS)],
    service: OrderServiceDep,
) -> list[OrderBookSummary]:
    return service.get_books([p.token_id for p in params])


@router.get("/prices-history")
def get_prices_history(
    market: str,
    service: OrderServiceDep,
    startTs: int | None = None,
    endTs: int | None = None,
    interval: str = "1d",
    fidelity: int = 0,
) -> dict:
    return service.get_prices_history(
        market, start_ts=startTs, end_ts=endTs, interval=interval, fidelity=fidelity
    )


@router.get("/midpoint")
def get_midpoint(token_id: str, service: OrderServiceDep) -> dict:
    return service.get_midpoint(token_id)


@router.get("/price")
def get_price(token_id: str, side: str, service: OrderServiceDep) -> dict:
    return service.get_price(token_id, side)


@router.get("/last-trade-price")
def get_last_trade_price(token_id: str, service: OrderServiceDep) -> dict:
    return service.get_last_trade_price(token_id)
