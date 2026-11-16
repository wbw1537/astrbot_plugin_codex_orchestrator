"""Compact, valid JSON for human commands; verbose evidence stays in Agent tools."""

from __future__ import annotations

import json
from typing import Any

from .bounds import bounded_value, json_size

CHAT_BYTES = 3800


def task_response(view: dict[str, Any], action: str, offset: int = 0) -> str:
    result = view.get("result")
    if not isinstance(result, dict):
        result = {"finalText": "" if result is None else str(result)}
    response = {key: view.get(key) for key in ("id", "env", "status", "thread_id", "turn_id")}
    response["interactions"] = [
        {
            **{key: item[key] for key in ("id", "method")},
            **(
                {"availableDecisions": item["availableDecisions"]}
                if "availableDecisions" in item
                else {}
            ),
            "details": bounded_value(item.get("params"), 1024),
        }
        for item in view.get("interactions", [])[:1]
    ]
    page_info = dict(view.get("interactionsPage", {}))
    more_interactions = len(view.get("interactions", [])) > 1 or page_info.get("hasMore", False)
    page_info["hasMore"] = more_interactions
    page_info["nextOffset"] = page_info.get("offset", 0) + 1 if more_interactions else None
    response["interactionsPage"] = page_info
    if response["interactionsPage"].get("hasMore"):
        response["nextStatusCommand"] = (
            f"/codex status {view['env']} {view['id']} {response['interactionsPage']['nextOffset']}"
        )
    if view.get("error") is not None:
        response["error"] = bounded_value(view["error"], 256)
    response["detailsTool"] = "codex_task"
    text = result.get("finalText", "") if isinstance(result, dict) else str(result)
    if action == "result":
        if offset > len(text):
            raise ValueError("result offset exceeds final text length")
        page = {"text": "", "offset": offset, "nextOffset": None, "hasMore": False}
        response["finalText"] = page
        if result.get("finalTextTruncated"):
            response["nativeTextTruncated"] = True
            response["nativeTextOriginalBytes"] = result.get("finalTextOriginalBytes")

        # Measure the complete envelope, including JSON escaping and the next command.
        def candidate(length):
            end = offset + length
            more = end < len(text)
            page.update(text=text[offset:end], nextOffset=end if more else None, hasMore=more)
            response.pop("nextResultCommand", None)
            if more:
                response["nextResultCommand"] = f"/codex result {view['env']} {view['id']} {end}"
            return json_size(response)

        low, high = 0, len(text) - offset
        if candidate(0) > CHAT_BYTES:
            raise ValueError("task metadata exceeds chat limit; use codex_task")
        if candidate(high) <= CHAT_BYTES:
            low = high
        else:
            while low < high:
                middle = (low + high + 1) // 2
                if candidate(middle) <= CHAT_BYTES:
                    low = middle
                else:
                    high = middle - 1
        if low == 0 and offset < len(text):
            raise ValueError("no room for result text; use codex_task")
        candidate(low)
    elif text:
        response["resultCommand"] = f"/codex result {view['env']} {view['id']}"
    if json_size(response) > CHAT_BYTES:
        raise ValueError("task metadata exceeds chat limit; use codex_task")
    return json.dumps(response, ensure_ascii=False, default=str)


def command_response(response: Any) -> str:
    if isinstance(response, dict) and "id" in response and "status" in response:
        return task_response(response, "status")
    return json.dumps(bounded_value(response, CHAT_BYTES), ensure_ascii=False, default=str)
