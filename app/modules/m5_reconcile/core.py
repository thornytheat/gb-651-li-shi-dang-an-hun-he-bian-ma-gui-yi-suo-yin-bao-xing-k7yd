"""M5 索引对账：按来源认领新旧档号别名，原字逐字比对。

核心约束：
- 档号是实体身份，比较时不做任何归一（不去前导零、不做全半角折叠、不去空白）。
- 别名只在同一来源内生效；同一字符串跨来源视为不同实体。
- 未登记的差异保留；多对一/一对多碰撞不自动合并，保持待裁决。
"""

from __future__ import annotations

import json
from collections import defaultdict, deque
from pathlib import Path

SAMPLE = {
    "index_ids": ["000123", "9007199254740993", "000404"],
    "normalized_ids": ["000123", "9007199254740993", "000777"],
    "quarantine": 1,
    "empty": 0,
}

_SAMPLE_FILE = Path(__file__).resolve().parents[3] / "sample_data" / "reconcile_records.json"

_ALLOWED_STATUS = {"pending", "active", "revoked"}


def load_sample_dataset() -> dict:
    """读取随仓样例；文件缺失时退回内置的单来源样例。"""
    try:
        data = json.loads(_SAMPLE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {
            "left": [{"source": "", "archive_id": value} for value in SAMPLE["index_ids"]],
            "right": [{"source": "", "archive_id": value} for value in SAMPLE["normalized_ids"]],
            "quarantine": SAMPLE["quarantine"],
            "empty": SAMPLE["empty"],
            "suggested_claims": [],
        }
    data.setdefault("left", [])
    data.setdefault("right", [])
    data.setdefault("quarantine", 0)
    data.setdefault("empty", 0)
    data.setdefault("suggested_claims", [])
    return data


def dataset_from_payload(payload: dict | None) -> dict:
    """把 API 载荷转成统一的记录集合，原值一律保留为字符串。"""
    payload = payload or {}
    if "left" in payload or "right" in payload:
        left = payload.get("left") or []
        right = payload.get("right") or []
    else:
        left = payload.get("index_ids") or []
        right = payload.get("normalized_ids") or []
    return {
        "left": left,
        "right": right,
        "quarantine": int(payload.get("quarantine", 0) or 0),
        "empty": int(payload.get("empty", 0) or 0),
        "suggested_claims": payload.get("suggested_claims") or [],
    }


def _text(value) -> str:
    return value if isinstance(value, str) else ("" if value is None else str(value))


def normalize_alias(value: dict) -> dict:
    """别名原值一律按字符串保留，前导零与全半角不做任何改写。"""
    return {
        "alias_id": _text(value.get("alias_id")),
        "source": _text(value.get("source")),
        "old_id": _text(value.get("old_id")),
        "new_id": _text(value.get("new_id")),
        "basis": _text(value.get("basis")),
        "status": _text(value.get("status")) or "pending",
        "version": int(value.get("version", 1) or 1),
    }


def _records(values, side: str) -> list[dict]:
    records = []
    for position, value in enumerate(values or []):
        if isinstance(value, dict):
            raw_id = value.get("archive_id", value.get("id"))
            if raw_id is None:
                raise ValueError("记录缺少 archive_id")
            archive_id = _text(raw_id)
            raw = value.get("raw", archive_id)
            record = {
                "source": _text(value.get("source")),
                "archive_id": archive_id,
                "source_line": value.get("source_line"),
                "raw": raw,
            }
        else:
            record = {"source": "", "archive_id": _text(value), "source_line": None, "raw": value}
        record.update({"side": side, "position": position})
        records.append(record)
    return records


def _pair(records_a, records_b, used_a, used_b, key_a, key_b) -> int:
    """按键顺序配对，返回配对数；同一键的多条记录按出现顺序一一消费。"""
    queues: dict = defaultdict(deque)
    for index, record in enumerate(records_b):
        if not used_b[index]:
            queues[key_b(record)].append(index)
    matched = 0
    for index, record in enumerate(records_a):
        if used_a[index]:
            continue
        queue = queues.get(key_a(record))
        if not queue:
            continue
        used_b[queue.popleft()] = True
        used_a[index] = True
        matched += 1
    return matched


def ambiguous_groups(aliases: list[dict]) -> list[dict]:
    """把同来源内互相竞争的别名聚成待裁决组；多对一与一对多不自动合并。"""
    live = [
        alias
        for alias in map(normalize_alias, aliases)
        if alias["status"] in ("pending", "active") and alias["old_id"] and alias["new_id"]
    ]
    if not live:
        return []
    parent = list(range(len(live)))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(i, j):
        root_i, root_j = find(i), find(j)
        if root_i != root_j:
            parent[root_j] = root_i

    by_key: dict = defaultdict(list)
    for index, alias in enumerate(live):
        by_key[("new", alias["source"], alias["new_id"])].append(index)
        by_key[("old", alias["source"], alias["old_id"])].append(index)
    for indexes in by_key.values():
        for other in indexes[1:]:
            union(indexes[0], other)

    clusters: dict = defaultdict(list)
    for index, alias in enumerate(live):
        clusters[find(index)].append(alias)

    groups = []
    for members in clusters.values():
        old_ids = {member["old_id"] for member in members}
        new_ids = {member["new_id"] for member in members}
        if len(members) < 2 or (len(old_ids) == 1 and len(new_ids) == 1):
            continue
        # 完全同映射（重复登记）不算碰撞；只有真正多对多/多对一才待裁决。
        if not any(member["status"] == "pending" for member in members):
            continue
        pairs = {(member["old_id"], member["new_id"]) for member in members}
        if len(pairs) < 2:
            continue
        if len(old_ids) > 1 and len(new_ids) == 1:
            reason = "MANY_TO_ONE"
        elif len(old_ids) == 1 and len(new_ids) > 1:
            reason = "ONE_TO_MANY"
        else:
            reason = "COMPLEX"
        groups.append(
            {
                "reason": reason,
                "source": members[0]["source"],
                "old_ids": sorted(old_ids),
                "new_ids": sorted(new_ids),
                "candidates": sorted(
                    (
                        {
                            "alias_id": member["alias_id"],
                            "source": member["source"],
                            "old_id": member["old_id"],
                            "new_id": member["new_id"],
                            "basis": member["basis"],
                            "status": member["status"],
                            "version": member["version"],
                        }
                        for member in members
                    ),
                    key=lambda item: (item["old_id"], item["new_id"], item["alias_id"]),
                ),
            }
        )
    return sorted(groups, key=lambda item: (item["source"], item["old_ids"], item["new_ids"]))


def claim_conflict(alias: dict, aliases: list[dict]) -> dict | None:
    """判断一条待认领别名是否与现有活别名在同一来源内碰撞。"""
    alias = normalize_alias(alias)
    for other in map(normalize_alias, aliases):
        if other["alias_id"] == alias["alias_id"]:
            continue
        if other["status"] not in ("pending", "active"):
            continue
        if other["source"] != alias["source"]:
            continue
        same_pair = other["old_id"] == alias["old_id"] and other["new_id"] == alias["new_id"]
        shares_new = other["new_id"] == alias["new_id"] and other["old_id"] != alias["old_id"]
        shares_old = other["old_id"] == alias["old_id"] and other["new_id"] != alias["new_id"]
        if same_pair or shares_new or shares_old:
            return {
                "reason": "MANY_TO_ONE" if shares_new else "ONE_TO_MANY" if shares_old else "DUPLICATE_CLAIM",
                "conflict_alias_id": other["alias_id"],
                "conflict_old_id": other["old_id"],
                "conflict_new_id": other["new_id"],
            }
    return None


def reconcile(index_ids, normalized_ids, quarantine=0, empty=0, aliases=None) -> dict:
    """对账。`index_ids` 是索引侧（旧档号），`normalized_ids` 是归一/馆方确认侧（新档号）。

    兼容旧的字符串列表入参；传入字典记录时可携带 `source` 与 `source_line`。
    档号逐字比较，别名仅在同一来源内生效。
    """
    left = _records(index_ids, "index")
    right = _records(normalized_ids, "normalized")
    quarantine = int(quarantine or 0)
    empty = int(empty or 0)
    alias_list = [normalize_alias(alias) for alias in (aliases or [])]
    active = sorted(
        (alias for alias in alias_list if alias["status"] == "active"),
        key=lambda item: (item["alias_id"], item["source"], item["old_id"], item["new_id"]),
    )

    sources = sorted({record["source"] for record in left} | {record["source"] for record in right})
    applied = []
    matched = 0
    missing_records: list[dict] = []
    orphan_records: list[dict] = []
    source_rows = []

    for source in sources:
        side_left = [record for record in left if record["source"] == source]
        side_right = [record for record in right if record["source"] == source]
        left_used = [False] * len(side_left)
        right_used = [False] * len(side_right)

        # 第一步：同来源、同原字，才算天然匹配。
        source_matched = _pair(
            side_left,
            side_right,
            left_used,
            right_used,
            lambda record: record["archive_id"],
            lambda record: record["archive_id"],
        )

        # 第二步：已认领的别名按稳定顺序消费剩余记录；不做全局模糊匹配。
        for alias in active:
            if alias["source"] != source:
                continue
            alias_hits = 0
            while True:
                left_index = next(
                    (i for i, record in enumerate(side_left) if not left_used[i] and record["archive_id"] == alias["old_id"]),
                    None,
                )
                right_index = next(
                    (i for i, record in enumerate(side_right) if not right_used[i] and record["archive_id"] == alias["new_id"]),
                    None,
                )
                if left_index is None or right_index is None:
                    break
                left_used[left_index] = True
                right_used[right_index] = True
                source_matched += 1
                alias_hits += 1
            if alias_hits:
                applied.append(
                    {
                        "alias_id": alias["alias_id"],
                        "source": alias["source"],
                        "old_id": alias["old_id"],
                        "new_id": alias["new_id"],
                        "basis": alias["basis"],
                        "matched_count": alias_hits,
                    }
                )

        side_missing = [record for i, record in enumerate(side_left) if not left_used[i]]
        side_orphan = [record for i, record in enumerate(side_right) if not right_used[i]]
        missing_records.extend(side_missing)
        orphan_records.extend(side_orphan)
        matched += source_matched
        source_rows.append(
            {
                "source": source,
                "left_count": len(side_left),
                "right_count": len(side_right),
                "matched_count": source_matched,
                "missing_count": len(side_missing),
                "orphan_count": len(side_orphan),
                "missing": sorted({record["archive_id"] for record in side_missing}),
                "orphan": sorted({record["archive_id"] for record in side_orphan}),
            }
        )

    def refs(records):
        return [
            {"source": record["source"], "archive_id": record["archive_id"], "source_line": record["source_line"]}
            for record in records
        ]

    index_count = len(left)
    output_count = len(right)
    input_count = index_count + quarantine + empty
    # 两侧各自守恒：索引侧每条记录要么被匹配、要么记为缺失；
    # 归一/确认侧同理。隔离与空记录只进入输入合计。
    balanced = index_count == matched + len(missing_records) and output_count == matched + len(orphan_records)

    return {
        "index_count": index_count,
        "output_count": output_count,
        "matched_count": matched,
        "missing_count": len(missing_records),
        "orphan_count": len(orphan_records),
        "missing": sorted({record["archive_id"] for record in missing_records}),
        "orphan": sorted({record["archive_id"] for record in orphan_records}),
        "missing_refs": refs(missing_records),
        "orphan_refs": refs(orphan_records),
        "sources": source_rows,
        "aliases_applied": applied,
        "aliases_pending": [alias for alias in alias_list if alias["status"] == "pending"],
        "ambiguous_groups": ambiguous_groups(alias_list),
        "quarantine_count": quarantine,
        "empty_count": empty,
        # 输入记录数 = 有效记录数 + 隔离记录数 + 空记录数（M5 的守恒式）。
        "input_count": index_count + quarantine + empty,
        "output_count_with_quarantine": output_count + quarantine + empty,
        "balanced": balanced,
        "reason": "DIFF" if missing_records or orphan_records else "OK",
    }


def run(payload) -> dict:
    data = SAMPLE | (payload or {})
    out = reconcile(
        data.get("index_ids", SAMPLE["index_ids"]),
        data.get("normalized_ids", SAMPLE["normalized_ids"]),
        data.get("quarantine", 0),
        data.get("empty", 0),
        data.get("aliases"),
    )
    return {
        "status": "READY",
        "input_count": out["input_count"],
        "valid_count": out["output_count"],
        **out,
    }
