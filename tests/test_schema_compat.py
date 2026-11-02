from typing import Annotated, Any, Literal

import pytest
from pydantic import BaseModel, Field, create_model

from tests.schema_compat import breaks

OLD: dict[str, Any] = {
    "status": (Literal["filled", "resting"], ...),
    "rank": (int, ...),
    "to_rank": (int, ...),
    "note": (Annotated[str, Field(max_length=200)], ...),
    "price": (float, ...),
    "level": (tuple[float, float], ...),
}


def _schema(**fields: Any) -> dict:
    return create_model("Reply", **fields).model_json_schema()


def _changed(**fields: Any) -> dict:
    return _schema(**{**OLD, **fields})


def test_the_same_schema_breaks_nothing():
    assert breaks(_schema(**OLD), _schema(**OLD)) == []


@pytest.mark.parametrize(
    "new",
    [
        _changed(extra=(str, ...)),
        _changed(status=(Literal["filled"], ...)),
        _changed(price=(int, ...)),
        _changed(note=(Annotated[str, Field(max_length=100)], ...)),
    ],
    ids=["added-field", "fewer-values", "integer-for-number", "tighter-bound"],
)
def test_what_old_clients_still_accept_passes(new: dict):
    assert breaks(_schema(**OLD), new) == []


@pytest.mark.parametrize(
    ("new", "found"),
    [
        (_schema(**{k: v for k, v in OLD.items() if k != "to_rank"}), ["$.to_rank: removed"]),
        (_changed(rank=(int | None, ...)), ["$.rank: null where integer was expected"]),
        (_changed(rank=(int | None, None)), ["$.rank: may be missing", "$.rank: null where integer was expected"]),
        (
            _changed(status=(Literal["filled", "resting", "cancelled"], ...)),
            ["$.status: ['cancelled'] outside ['filled', 'resting']"],
        ),
        (_changed(status=(str, ...)), ["$.status: any value outside ['filled', 'resting']"]),
        (_changed(price=(str, ...)), ["$.price: string where number was expected"]),
        (_changed(note=(str, ...)), ["$.note: maxLength none looser than 200"]),
        (_changed(level=(tuple[float, str], ...)), ["$.level[1]: string where number was expected"]),
    ],
    ids=["removed", "nullable", "optional", "new-literal-value", "literal-to-str", "new-type", "looser-bound", "tuple-item"],
)
def test_what_old_clients_reject_is_named(new: dict, found: list[str]):
    assert breaks(_schema(**OLD), new) == found


def test_a_change_inside_a_nested_model_names_its_path():
    class Holding(BaseModel):
        shares: float
        outcome: str

    class Thinner(BaseModel):
        shares: float

    assert breaks(_schema(positions=(list[Holding], ...)), _schema(positions=(list[Thinner], ...))) == [
        "$.positions[].outcome: removed"
    ]
