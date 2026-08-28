"""Durable workflow vocabulary shared by HTTP handlers and background workers.

Keep state names and message-target extraction outside ``app.py`` so the
business workflow can evolve without coupling it to FastAPI route code.
"""

from __future__ import annotations

import json
from typing import Any, Iterable


JOB_STATE_LABELS = {
    "discovered": "发现资源",
    "unlocking": "正在解锁",
    "submitted": "已提交",
    "transferred": "已确认转存",
    "organizing": "等待整理",
    "waiting_library": "等待入库",
    "ingested": "已入库",
    "failed": "处理失败",
    "cancelled": "已取消",
}

ACTIVE_JOB_STATES = {
    "discovered",
    "unlocking",
    "submitted",
    "transferred",
    "organizing",
    "waiting_library",
    "failed",
}

TERMINAL_JOB_STATES = {"ingested", "cancelled"}


def episode_numbers_json(values: Iterable[int]) -> str:
    return json.dumps(
        sorted({int(value) for value in values if int(value) > 0}),
        separators=(",", ":"),
    )


def episode_numbers_from_json(value: Any) -> list[int]:
    try:
        values = json.loads(str(value or "[]"))
    except (TypeError, ValueError):
        return []
    if not isinstance(values, list):
        return []
    output: list[int] = []
    for item in values:
        try:
            number = int(item)
        except (TypeError, ValueError):
            continue
        if number > 0:
            output.append(number)
    return sorted(set(output))


def message_target_hints(payload: dict[str, Any]) -> dict[str, set[Any]]:
    """Extract conservative subscription/TMDB hints from an HDHive message.

    The OpenAPI may wrap event data differently across message versions, so we
    inspect only explicit identifier keys and never infer identity from titles.
    """

    subscription_ids: set[int] = set()
    tmdb_ids: set[int] = set()
    target_keys: set[str] = set()
    visited: set[int] = set()

    def walk(value: Any, depth: int = 0) -> None:
        if depth > 5:
            return
        if isinstance(value, dict):
            marker = id(value)
            if marker in visited:
                return
            visited.add(marker)
            for raw_key, child in value.items():
                key = str(raw_key or "").strip().lower()
                if key in {"subscription_id", "subscriptionid"}:
                    try:
                        number = int(child)
                    except (TypeError, ValueError):
                        number = 0
                    if number > 0:
                        subscription_ids.add(number)
                elif key in {"tmdb_id", "tmdbid", "tmdb"}:
                    try:
                        number = int(child)
                    except (TypeError, ValueError):
                        number = 0
                    if number > 0:
                        tmdb_ids.add(number)
                elif key in {"target_key", "targetkey"}:
                    text = str(child or "").strip()
                    if text:
                        target_keys.add(text)
                if isinstance(child, (dict, list, tuple)):
                    walk(child, depth + 1)
        elif isinstance(value, (list, tuple)):
            for child in value:
                walk(child, depth + 1)

    walk(payload)
    return {
        "subscription_ids": subscription_ids,
        "tmdb_ids": tmdb_ids,
        "target_keys": target_keys,
    }

