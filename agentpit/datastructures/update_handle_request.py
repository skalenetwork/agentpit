import re
from typing import Annotated

from pydantic import AfterValidator, BaseModel


def _handle_format(v: str) -> str:
    value = v.strip()
    if not re.fullmatch(r"[a-zA-Z0-9_]{1,15}", value):
        raise ValueError("handle must be 1-15 chars, alphanumeric or underscore")
    return value


Handle = Annotated[str, AfterValidator(_handle_format)]


class UpdateHandleRequest(BaseModel):
    handle: Handle
