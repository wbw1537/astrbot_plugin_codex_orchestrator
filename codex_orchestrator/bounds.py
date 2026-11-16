"""Byte bounds include JSON escaping and truncation metadata."""

from __future__ import annotations

import json
from typing import Any


def json_size(value: Any) -> int:
    # ASCII escaping is conservative for both default and UTF-8 JSON consumers.
    return len(json.dumps(value, ensure_ascii=True, default=str).encode("utf-8"))


def preview_record(text: str, metadata: dict[str, Any], max_bytes: int) -> dict[str, Any]:
    def record(length: int) -> dict[str, Any]:
        return {**metadata, "preview": text[:length] + "…"}

    if json_size(record(0)) > max_bytes:
        raise ValueError("byte budget is too small for truncation metadata")
    low, high = 0, len(text)
    while low < high:
        middle = (low + high + 1) // 2
        if json_size(record(middle)) <= max_bytes:
            low = middle
        else:
            high = middle - 1
    return record(low)


def bounded_value(value: Any, max_bytes: int) -> Any:
    encoded = json.dumps(value, ensure_ascii=False, default=str)
    if json_size(value) <= max_bytes:
        return value
    return preview_record(
        encoded,
        {"truncated": True, "originalBytes": len(encoded.encode("utf-8"))},
        max_bytes,
    )


def bounded_text(text: str, max_bytes: int) -> str:
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text
    marker = "…[truncated]"
    budget = max_bytes - len(marker.encode("utf-8"))
    if budget < 0:
        raise ValueError("byte budget is too small for truncation marker")
    return encoded[:budget].decode("utf-8", errors="ignore") + marker
