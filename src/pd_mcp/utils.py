"""Small shared helpers: pagination, naming checks, id generation."""

from __future__ import annotations

import datetime as _dt
import re
from typing import Any, Dict, List, Optional, Sequence

_SNAKE = re.compile(r"^[a-z][a-z0-9_]*$")


def now_iso() -> str:
    return _dt.datetime.now().isoformat(timespec="seconds")


def paginate(items: Sequence[Dict[str, Any]], page: int = 1, page_size: int = 50,
             max_page_size: int = 500) -> Dict[str, Any]:
    page = max(1, int(page or 1))
    page_size = int(page_size or 50)
    page_size = max(1, min(page_size, max_page_size))
    total = len(items)
    start = (page - 1) * page_size
    chunk = list(items[start:start + page_size])
    return {
        "items": chunk,
        "total": total,
        "page": page,
        "page_size": page_size,
        "pages": (total + page_size - 1) // page_size if total else 0,
        "has_more": start + page_size < total,
    }


def is_snake_case(value: str) -> bool:
    return bool(_SNAKE.match(value or ""))


def to_snake_case(value: str) -> str:
    value = re.sub(r"[^0-9a-zA-Z_]+", "_", value or "")
    value = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", value)
    return re.sub(r"_+", "_", value).strip("_").lower()


def short_id(prefix: str, used: set) -> str:
    n = 1
    while True:
        candidate = f"{prefix}{n}"
        if candidate not in used:
            used.add(candidate)
            return candidate
        n += 1


def coerce_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "yes", "on", "y")


def match_filter(obj: Dict[str, Any], query: str, fields: Sequence[str]) -> bool:
    if not query:
        return True
    q = query.lower()
    return any(q in str(obj.get(f) or "").lower() for f in fields)


def sum_objects(*counts: int) -> int:
    return sum(c or 0 for c in counts)
