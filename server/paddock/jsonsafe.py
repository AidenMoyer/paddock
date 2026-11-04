"""NaN and infinity aren't JSON. They reach us from files: Android's photo picker blanks a photo's location by writing
0/0, which reads back as NaN. One such number must never turn a page into a server error."""

from __future__ import annotations

import math
from typing import Any

from fastapi.responses import JSONResponse


def finite(value: Any) -> Any:
    """The same value with every number that JSON can't hold replaced by null."""
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {k: finite(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [finite(v) for v in value]
    return value


class SafeJSONResponse(JSONResponse):
    """Catalog rows scanned by an older version can still hold these, so they're cleaned on the way out as well."""

    def render(self, content: Any) -> bytes:
        try:
            return super().render(content)
        except ValueError:
            return super().render(finite(content))
