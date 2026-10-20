from pydantic import BaseModel, Field


class BookParams(BaseModel):
    """One element of the `POST /books` batch request."""

    # A token id is a uint256 in decimal: at most 78 digits.
    token_id: str = Field(min_length=1, max_length=100)
