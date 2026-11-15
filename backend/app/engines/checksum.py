from __future__ import annotations

import hashlib
import json
from typing import Any

# 指纹参与字段：生成与核验必须使用完全相同的字段集合与序列化方式。
FINGERPRINT_FIELDS = ("business_key", "title", "amount", "qty", "event_ts", "revision")
SUMMARY_FIELDS = FINGERPRINT_FIELDS[1:]


def item_payload(item: dict[str, Any]) -> dict[str, Any]:
    return {field: item.get(field) for field in FINGERPRINT_FIELDS}


def item_fingerprint(item: dict[str, Any]) -> str:
    raw = json.dumps(
        item_payload(item),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def pack_fingerprint(items: list[dict[str, Any]]) -> str:
    """SHA256 over sorted item fingerprints (by business_key)."""
    sorted_items = sorted(items, key=lambda x: str(x.get("business_key", "")))
    digests = [item_fingerprint(it) for it in sorted_items]
    joined = "\n".join(digests)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


def items_from_pack_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """把落库条目还原为参与指纹计算的条目结构。

    生成证据包时摘要里保存的正是参与指纹的字段，因此按同样的字段还原后
    重算出的指纹应与生成时一致；条目增、删、改任一发生都会改变结果。
    """
    items: list[dict[str, Any]] = []
    for row in rows:
        summary = row.get("summary")
        if not isinstance(summary, dict):
            summary = {}
        item: dict[str, Any] = {"business_key": row.get("business_key")}
        for field in SUMMARY_FIELDS:
            item[field] = summary.get(field)
        items.append(item)
    return items
