"""M5 别名认领台账。

台账按来源记录「旧档号 → 新档号」的认领关系与依据，并提供：
- 认领 / 拆分 / 撤销三种决定，差异计数与决定在同一事务内落库；
- 决定键幂等：重试返回首次结果，不重复认领；
- 乐观版本：旧决定重放得到 STALE_VERSION，不覆盖新裁决；
- 冲突登记：并发竞争失败的一方在 `alias_conflicts` 中可见；
- 决定日志：先记 intent，再在事务内应用；中断重启后可续跑。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from app.errors import DomainError
from app.manifest import canonical_digest

from . import core



def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _text(value) -> str:
    return value if isinstance(value, str) else ("" if value is None else str(value))


def _alias_id(source: str, old_id: str, new_id: str) -> str:
    raw = f"{source}\x1f{old_id}\x1f{new_id}".encode("utf-8")
    return "alias-" + hashlib.sha256(raw).hexdigest()[:12]


class Ledger:
    def __init__(self, path: str):
        self.path = path
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()
        self.recover()

    # ---- 连接与结构 -------------------------------------------------
    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA busy_timeout=5000")
        return db

    def _init_schema(self) -> None:
        with self._connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute(
                """CREATE TABLE IF NOT EXISTS alias_claims (
                    alias_id TEXT PRIMARY KEY, source TEXT NOT NULL,
                    old_id TEXT NOT NULL, new_id TEXT NOT NULL,
                    basis TEXT NOT NULL, status TEXT NOT NULL,
                    version INTEGER NOT NULL, decision_key TEXT,
                    updated_at TEXT NOT NULL)"""
            )
            db.execute(
                """CREATE TABLE IF NOT EXISTS alias_revisions (
                    rev_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    alias_id TEXT NOT NULL, source TEXT NOT NULL,
                    old_id TEXT NOT NULL, new_id TEXT NOT NULL,
                    basis TEXT NOT NULL, status TEXT NOT NULL,
                    version INTEGER NOT NULL, kind TEXT NOT NULL,
                    decision_key TEXT, diff_before TEXT, diff_after TEXT,
                    decided_at TEXT NOT NULL)"""
            )
            db.execute(
                """CREATE TABLE IF NOT EXISTS alias_operations (
                    decision_key TEXT PRIMARY KEY, kind TEXT NOT NULL,
                    alias_id TEXT NOT NULL, expected_version INTEGER,
                    request TEXT NOT NULL, state TEXT NOT NULL,
                    result TEXT, error TEXT, created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL)"""
            )
            db.execute(
                """CREATE TABLE IF NOT EXISTS alias_conflicts (
                    conflict_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    alias_id TEXT, source TEXT, reason TEXT,
                    decision_key TEXT, expected_version INTEGER,
                    actual_version INTEGER, detail TEXT,
                    created_at TEXT NOT NULL)"""
            )

    # ---- 查询 -------------------------------------------------------
    @staticmethod
    def _rows(db, sql: str, args=()) -> list[dict]:
        return [dict(row) for row in db.execute(sql, args).fetchall()]

    def aliases(self) -> list[dict]:
        with self._connect() as db:
            return self._aliases(db)

    @staticmethod
    def _aliases(db) -> list[dict]:
        return [
            dict(row)
            for row in db.execute(
                "SELECT alias_id, source, old_id, new_id, basis, status, version FROM alias_claims ORDER BY alias_id"
            ).fetchall()
        ]

    def revisions(self, alias_id: str | None = None) -> list[dict]:
        with self._connect() as db:
            if alias_id:
                rows = db.execute(
                    "SELECT * FROM alias_revisions WHERE alias_id=? ORDER BY rev_id", (alias_id,)
                ).fetchall()
            else:
                rows = db.execute("SELECT * FROM alias_revisions ORDER BY rev_id").fetchall()
        out = []
        for row in rows:
            item = dict(row)
            for key in ("diff_before", "diff_after"):
                item[key] = json.loads(item[key]) if item[key] else None
            out.append(item)
        return out

    def operations(self) -> list[dict]:
        with self._connect() as db:
            rows = db.execute("SELECT * FROM alias_operations ORDER BY created_at, decision_key").fetchall()
        out = []
        for row in rows:
            item = dict(row)
            item["request"] = json.loads(item["request"])
            item["result"] = json.loads(item["result"]) if item["result"] else None
            out.append(item)
        return out

    def conflicts(self) -> list[dict]:
        with self._connect() as db:
            rows = db.execute("SELECT * FROM alias_conflicts ORDER BY conflict_id").fetchall()
        return [dict(row) for row in rows]

    def _alias(self, db, alias_id: str) -> dict | None:
        row = db.execute(
            "SELECT alias_id, source, old_id, new_id, basis, status, version FROM alias_claims WHERE alias_id=?",
            (alias_id,),
        ).fetchone()
        return dict(row) if row else None

    # ---- 恢复 -------------------------------------------------------
    def recover(self) -> None:
        """中断重启：未完成的 intent 标记为 interrupted，等待同决定键重试续跑。"""
        now = _now()
        with self._connect() as db:
            db.execute(
                """UPDATE alias_operations SET state='interrupted', updated_at=?
                   WHERE state='intent'
                     AND decision_key NOT IN (SELECT decision_key FROM alias_revisions)""",
                (now,),
            )
            db.execute(
                """UPDATE alias_operations SET state='applied', updated_at=?
                   WHERE state='intent'
                     AND decision_key IN (SELECT decision_key FROM alias_revisions)""",
                (now,),
            )

    # ---- 种子 -------------------------------------------------------
    def seed(self, claims: list[dict]) -> int:
        """把馆方建议登记为待认领项；幂等，不改变已有决定。"""
        inserted = 0
        now = _now()
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                for value in claims or []:
                    source = _text(value.get("source"))
                    old_id = _text(value.get("old_id"))
                    new_id = _text(value.get("new_id"))
                    basis = _text(value.get("basis"))
                    alias_id = _text(value.get("alias_id")) or _alias_id(source, old_id, new_id)
                    if not (source and old_id and new_id):
                        continue
                    cursor = db.execute(
                        """INSERT OR IGNORE INTO alias_claims
                           (alias_id, source, old_id, new_id, basis, status, version, decision_key, updated_at)
                           VALUES (?, ?, ?, ?, ?, 'pending', 1, NULL, ?)""",
                        (alias_id, source, old_id, new_id, basis, now),
                    )
                    if cursor.rowcount:
                        inserted += 1
                        db.execute(
                            """INSERT INTO alias_revisions
                               (alias_id, source, old_id, new_id, basis, status, version,
                                kind, decision_key, diff_before, diff_after, decided_at)
                               VALUES (?, ?, ?, ?, ?, 'pending', 1, 'seed', NULL, NULL, NULL, ?)""",
                            (alias_id, source, old_id, new_id, basis, now),
                        )
                db.execute("COMMIT")
            except Exception:
                db.execute("ROLLBACK")
                raise
        return inserted

    # ---- 差异计数 ---------------------------------------------------
    @staticmethod
    def diff_summary(dataset: dict, aliases: list[dict]) -> dict:
        out = core.reconcile(
            dataset.get("left") or [],
            dataset.get("right") or [],
            dataset.get("quarantine", 0),
            dataset.get("empty", 0),
            aliases,
        )
        return {
            "matched_count": out["matched_count"],
            "missing_count": out["missing_count"],
            "orphan_count": out["orphan_count"],
            "reason": out["reason"],
            "sources": [
                {
                    "source": row["source"],
                    "matched_count": row["matched_count"],
                    "missing_count": row["missing_count"],
                    "orphan_count": row["orphan_count"],
                }
                for row in out["sources"]
            ],
        }

    # ---- 决定执行 ---------------------------------------------------
    def claim(self, request: dict, dataset: dict) -> dict:
        alias_id = _text(request.get("alias_id"))
        source = _text(request.get("source"))
        old_id = _text(request.get("old_id"))
        new_id = _text(request.get("new_id"))
        basis = _text(request.get("basis"))
        if not (source and old_id and new_id):
            raise DomainError("INVALID_ALIAS", "认领需要来源、旧档号与新档号")
        if not basis:
            raise DomainError("BASIS_REQUIRED", "认领必须记录馆方确认依据")
        alias_id = alias_id or _alias_id(source, old_id, new_id)
        request = {
            "kind": "claim",
            "alias_id": alias_id,
            "source": source,
            "old_id": old_id,
            "new_id": new_id,
            "basis": basis,
            "expected_version": request.get("expected_version"),
            "decision_key": request.get("decision_key"),
        }
        return self._decide(request, dataset, self._apply_claim)

    def undo(self, alias_id: str, request: dict, dataset: dict) -> dict:
        request = {
            "kind": "undo",
            "alias_id": alias_id,
            "basis": _text(request.get("basis")),
            "expected_version": request.get("expected_version"),
            "decision_key": request.get("decision_key"),
        }
        return self._decide(request, dataset, self._apply_undo)

    def split(self, alias_id: str, parts: list[dict], request: dict, dataset: dict) -> dict:
        cleaned = []
        for position, part in enumerate(parts or [], 1):
            old_id = _text(part.get("old_id"))
            new_id = _text(part.get("new_id"))
            if not (old_id and new_id):
                raise DomainError("INVALID_SPLIT", "拆分各部分都需要旧档号与新档号")
            cleaned.append(
                {
                    "alias_id": _text(part.get("alias_id")) or f"{alias_id}-p{position}",
                    "old_id": old_id,
                    "new_id": new_id,
                    "basis": _text(part.get("basis")) or _text(request.get("basis")),
                }
            )
        if len(cleaned) < 2:
            raise DomainError("INVALID_SPLIT", "拆分至少需要两部分")
        request = {
            "kind": "split",
            "alias_id": alias_id,
            "parts": cleaned,
            "basis": _text(request.get("basis")),
            "expected_version": request.get("expected_version"),
            "decision_key": request.get("decision_key"),
        }
        return self._decide(request, dataset, self._apply_split)

    def _decide(self, request: dict, dataset: dict, apply) -> dict:
        decision_key = _text(request.get("decision_key")) or "auto-" + canonical_digest(request)[:16]
        request = request | {"decision_key": decision_key}
        canonical = json.dumps(request, ensure_ascii=False, sort_keys=True)
        expected_version = request.get("expected_version")
        expected_version = None if expected_version in (None, "") else int(expected_version)
        now = _now()

        # 第一步：决定日志先落 intent，崩溃后由 recover() 识别并允许同键续跑。
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute(
                "SELECT request FROM alias_operations WHERE decision_key=?", (decision_key,)
            ).fetchone()
            if existing and existing["request"] != canonical:
                db.execute("ROLLBACK")
                raise DomainError("DECISION_KEY_REUSED", "同一决定键提交了不同内容")
            if not existing:
                db.execute(
                    """INSERT INTO alias_operations
                       (decision_key, kind, alias_id, expected_version, request, state,
                        result, error, created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, 'intent', NULL, NULL, ?, ?)""",
                    (decision_key, request["kind"], request["alias_id"], expected_version, canonical, now, now),
                )
            db.execute("COMMIT")

        # 第二步：串行事务内校验版本、应用决定、计算差异并提交。
        db = self._connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            op = db.execute("SELECT * FROM alias_operations WHERE decision_key=?", (decision_key,)).fetchone()
            if op["state"] == "applied" and op["result"]:
                db.execute("COMMIT")
                return json.loads(op["result"]) | {"operation_state": "REPLAYED"}
            if op["state"] == "failed" and op["error"]:
                # 失败的决定保持失败；重试不得在更新裁决之后翻身。
                db.execute("COMMIT")
                raise DomainError(op["error"], "该决定已判定失败，请以当前版本重新提交新的决定键")
            applied = db.execute(
                "SELECT * FROM alias_revisions WHERE decision_key=? ORDER BY rev_id DESC LIMIT 1",
                (decision_key,),
            ).fetchone()
            if applied:
                # 中断发生在“已落库但尚未标记”之间：以既有修订为准，绝不重复认领。
                result = {
                    "status": "APPLIED",
                    "action": request["kind"],
                    "already_applied": True,
                    "diff_after": json.loads(applied["diff_after"]) if applied["diff_after"] else None,
                }
                alias = self._alias(db, request["alias_id"])
                if alias:
                    result["alias"] = alias
                    result["evidence"] = self._evidence(db, request["alias_id"])
                db.execute(
                    "UPDATE alias_operations SET state='applied', result=?, error=NULL, updated_at=? WHERE decision_key=?",
                    (json.dumps(result, ensure_ascii=False, sort_keys=True), _now(), decision_key),
                )
                db.execute("COMMIT")
                return result | {"operation_state": "RECOVERED"}
            current = self._alias(db, request["alias_id"])
            current_version = current["version"] if current else 0
            if expected_version is not None and expected_version != current_version:
                self._fail(db, decision_key, request, "STALE_VERSION", current_version)
                db.execute("COMMIT")
                raise DomainError(
                    "STALE_VERSION",
                    f"提交版本 {expected_version} 已过期，当前版本 {current_version}",
                )
            try:
                result = apply(db, request, dataset, current)
            except DomainError as exc:
                self._fail(db, decision_key, request, exc.code, current_version, exc.message)
                db.execute("COMMIT")
                raise
            db.execute(
                "UPDATE alias_operations SET state='applied', result=?, error=NULL, updated_at=? WHERE decision_key=?",
                (json.dumps(result, ensure_ascii=False, sort_keys=True), _now(), decision_key),
            )
            db.execute("COMMIT")
            return result | {"operation_state": result.get("operation_state", "APPLIED")}
        except Exception:
            try:
                db.execute("ROLLBACK")
            except sqlite3.OperationalError:
                pass
            raise
        finally:
            db.close()

    def _fail(self, db, decision_key, request, code, current_version, message="") -> None:
        db.execute(
            "UPDATE alias_operations SET state='failed', error=?, updated_at=? WHERE decision_key=?",
            (code, _now(), decision_key),
        )
        if code in ("STALE_VERSION", "AMBIGUOUS_CLAIM", "ONE_TO_MANY", "MANY_TO_ONE", "DUPLICATE_CLAIM"):
            db.execute(
                """INSERT INTO alias_conflicts
                   (alias_id, source, reason, decision_key, expected_version, actual_version, detail, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    request.get("alias_id"),
                    request.get("source"),
                    code,
                    decision_key,
                    None if request.get("expected_version") in (None, "") else int(request["expected_version"]),
                    current_version,
                    message,
                    _now(),
                ),
            )

    # ---- 三种决定的落库 ---------------------------------------------
    def _apply_claim(self, db, request, dataset, current) -> dict:
        before_aliases = self._aliases(db)
        diff_before = self.diff_summary(dataset, before_aliases)
        conflict = self._conflict(db, request["source"], request["old_id"], request["new_id"], request["alias_id"])
        if conflict:
            raise DomainError(
                conflict,
                f"与 {request['source']} 内既有认领冲突（{conflict}）",
            )
        if current:
            if current["status"] == "active":
                # 并发重复认领：第二个提交者必须看到冲突，而不是悄悄抬高版本。
                raise DomainError("DUPLICATE_CLAIM", "该旧档号已由其他决定认领，请核对当前版本")
            version = current["version"] + 1
            db.execute(
                """UPDATE alias_claims SET status='active', basis=?, version=?, decision_key=?, updated_at=?
                   WHERE alias_id=?""",
                (request["basis"], version, request["decision_key"], _now(), request["alias_id"]),
            )
        else:
            version = 1
            db.execute(
                """INSERT INTO alias_claims
                   (alias_id, source, old_id, new_id, basis, status, version, decision_key, updated_at)
                   VALUES (?, ?, ?, ?, ?, 'active', ?, ?, ?)""",
                (
                    request["alias_id"],
                    request["source"],
                    request["old_id"],
                    request["new_id"],
                    request["basis"],
                    version,
                    request["decision_key"],
                    _now(),
                ),
            )
        alias = self._alias(db, request["alias_id"])
        diff_after = self.diff_summary(dataset, self._aliases(db))
        self._revision(db, alias, "claim", request["decision_key"], diff_before, diff_after)
        return {
            "status": "APPLIED",
            "action": "claim",
            "alias": alias,
            "evidence": self._evidence(db, alias["alias_id"]),
            "diff_before": diff_before,
            "diff_after": diff_after,
            "delta": {
                "matched": diff_after["matched_count"] - diff_before["matched_count"],
                "missing": diff_after["missing_count"] - diff_before["missing_count"],
                "orphan": diff_after["orphan_count"] - diff_before["orphan_count"],
            },
        }

    def _apply_undo(self, db, request, dataset, current) -> dict:
        if not current:
            raise DomainError("ALIAS_NOT_FOUND", "认领不存在，无法撤销")
        before_aliases = self._aliases(db)
        diff_before = self.diff_summary(dataset, before_aliases)
        if current["status"] == "revoked":
            return {
                "status": "ALREADY_REVOKED",
                "action": "undo",
                "alias": current,
                "evidence": self._evidence(db, current["alias_id"]),
                "diff_before": diff_before,
                "diff_after": diff_before,
                "delta": {"matched": 0, "missing": 0, "orphan": 0},
            }
        basis = request.get("basis") or current["basis"]
        version = current["version"] + 1
        db.execute(
            """UPDATE alias_claims SET status='revoked', basis=?, version=?, decision_key=?, updated_at=?
               WHERE alias_id=?""",
            (basis, version, request["decision_key"], _now(), current["alias_id"]),
        )
        revoked_parts = []
        if current["status"] == "split":
            # 撤销拆分：一并撤销同一次拆分裂出的各部分，避免留下悬空认领。
            parts = db.execute(
                "SELECT alias_id FROM alias_claims WHERE decision_key=? AND alias_id != ?",
                (current["decision_key"], current["alias_id"]),
            ).fetchall()
            for part in parts:
                part_alias = self._alias(db, part["alias_id"])
                db.execute(
                    """UPDATE alias_claims SET status='revoked', version=version+1, decision_key=?, updated_at=?
                       WHERE alias_id=?""",
                    (request["decision_key"], _now(), part["alias_id"]),
                )
                revoked_parts.append(self._alias(db, part["alias_id"]))
        alias = self._alias(db, current["alias_id"])
        diff_after = self.diff_summary(dataset, self._aliases(db))
        self._revision(db, alias, "undo", request["decision_key"], diff_before, diff_after)
        for part in revoked_parts:
            self._revision(db, part, "undo-part", request["decision_key"], None, diff_after)
        return {
            "status": "APPLIED",
            "action": "undo",
            "alias": alias,
            "revoked_parts": revoked_parts,
            "evidence": self._evidence(db, alias["alias_id"]),
            "diff_before": diff_before,
            "diff_after": diff_after,
            "delta": {
                "matched": diff_after["matched_count"] - diff_before["matched_count"],
                "missing": diff_after["missing_count"] - diff_before["missing_count"],
                "orphan": diff_after["orphan_count"] - diff_before["orphan_count"],
            },
        }

    def _apply_split(self, db, request, dataset, current) -> dict:
        if not current:
            raise DomainError("ALIAS_NOT_FOUND", "认领不存在，无法拆分")
        if current["status"] in ("split",):
            raise DomainError("ALREADY_SPLIT", "该认领已经拆分")
        before_aliases = self._aliases(db)
        diff_before = self.diff_summary(dataset, before_aliases)
        parts = request["parts"]
        seen_pairs = set()
        for part in parts:
            pair = (part["old_id"], part["new_id"])
            if pair in seen_pairs:
                raise DomainError("INVALID_SPLIT", "拆分各部分映射重复")
            seen_pairs.add(pair)
            conflict = self._conflict(
                db, current["source"], part["old_id"], part["new_id"], part["alias_id"], ignore={current["alias_id"]}
            )
            if conflict:
                raise DomainError(conflict, f"拆分的 {part['old_id']}→{part['new_id']} 与既有认领冲突")
        for part in parts:
            existing = self._alias(db, part["alias_id"])
            if existing and existing["alias_id"] != current["alias_id"]:
                raise DomainError("INVALID_SPLIT", f"拆分目标 {part['alias_id']} 已存在")
        version = current["version"] + 1
        db.execute(
            """UPDATE alias_claims SET status='split', basis=?, version=?, decision_key=?, updated_at=?
               WHERE alias_id=?""",
            (request.get("basis") or current["basis"], version, request["decision_key"], _now(), current["alias_id"]),
        )
        created = []
        for part in parts:
            db.execute(
                """INSERT INTO alias_claims
                   (alias_id, source, old_id, new_id, basis, status, version, decision_key, updated_at)
                   VALUES (?, ?, ?, ?, ?, 'active', 1, ?, ?)""",
                (
                    part["alias_id"],
                    current["source"],
                    part["old_id"],
                    part["new_id"],
                    part["basis"] or current["basis"],
                    request["decision_key"],
                    _now(),
                ),
            )
            created.append(self._alias(db, part["alias_id"]))
        diff_after = self.diff_summary(dataset, self._aliases(db))
        for alias in created:
            self._revision(db, alias, "split-part", request["decision_key"], None, diff_after)
        self._revision(db, self._alias(db, current["alias_id"]), "split", request["decision_key"], diff_before, diff_after)
        return {
            "status": "APPLIED",
            "action": "split",
            "alias": self._alias(db, current["alias_id"]),
            "created": created,
            "evidence": [
                item for alias in [current["alias_id"]] + [part["alias_id"] for part in parts]
                for item in self._evidence(db, alias)
            ],
            "diff_before": diff_before,
            "diff_after": diff_after,
            "delta": {
                "matched": diff_after["matched_count"] - diff_before["matched_count"],
                "missing": diff_after["missing_count"] - diff_before["missing_count"],
                "orphan": diff_after["orphan_count"] - diff_before["orphan_count"],
            },
        }

    # ---- 辅助 -------------------------------------------------------
    @staticmethod
    def _conflict(db, source, old_id, new_id, alias_id, ignore=None) -> str | None:
        ignore = set(ignore or ()) | {alias_id}
        rows = db.execute(
            "SELECT alias_id, old_id, new_id FROM alias_claims WHERE source=? AND status IN ('pending','active')",
            (source,),
        ).fetchall()
        for row in rows:
            if row["alias_id"] in ignore:
                continue
            if row["old_id"] == old_id and row["new_id"] == new_id:
                return "DUPLICATE_CLAIM"
            if row["new_id"] == new_id and row["old_id"] != old_id:
                return "MANY_TO_ONE"
            if row["old_id"] == old_id and row["new_id"] != new_id:
                return "ONE_TO_MANY"
        return None

    def _revision(self, db, alias, kind, decision_key, diff_before, diff_after) -> None:
        db.execute(
            """INSERT INTO alias_revisions
               (alias_id, source, old_id, new_id, basis, status, version, kind,
                decision_key, diff_before, diff_after, decided_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                alias["alias_id"],
                alias["source"],
                alias["old_id"],
                alias["new_id"],
                alias["basis"],
                alias["status"],
                alias["version"],
                kind,
                decision_key,
                json.dumps(diff_before, ensure_ascii=False, sort_keys=True) if diff_before else None,
                json.dumps(diff_after, ensure_ascii=False, sort_keys=True) if diff_after else None,
                _now(),
            ),
        )

    @staticmethod
    def _evidence(db, alias_id: str) -> list[dict]:
        rows = db.execute(
            """SELECT rev_id, alias_id, basis, status, version, kind, decided_at
               FROM alias_revisions WHERE alias_id=? ORDER BY rev_id""",
            (alias_id,),
        ).fetchall()
        return [dict(row) for row in rows]
