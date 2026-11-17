from __future__ import annotations

from fastapi import Request

from app.errors import DomainError

from . import core
from .ledger import Ledger


def ledger_for(request: Request) -> Ledger:
    """按应用的数据库路径复用同一个台账实例。"""
    store = getattr(request.app.state, "alias_ledger", None)
    path = request.app.state.settings.db_path
    if store is None or store.path != path:
        store = Ledger(path)
        request.app.state.alias_ledger = store
    return store


def dataset(payload: dict | None) -> dict:
    if payload:
        nested = payload.get("dataset")
        if isinstance(nested, dict):
            return core.dataset_from_payload(nested)
        if "left" in payload or "right" in payload or "index_ids" in payload:
            return core.dataset_from_payload(payload)
    return core.load_sample_dataset()


def seed(payload: dict | None, ledger: Ledger) -> int:
    data = dataset(payload)
    claims = list(data.get("suggested_claims") or [])
    if payload:
        claims += payload.get("claims") or payload.get("suggested_claims") or []
    return ledger.seed(claims)


def report(payload: dict | None, ledger: Ledger) -> dict:
    data = dataset(payload)
    aliases = ledger.aliases()
    out = core.reconcile(
        data.get("left") or [],
        data.get("right") or [],
        data.get("quarantine", 0),
        data.get("empty", 0),
        aliases,
    )
    return {
        "status": "READY",
        "input_count": out["input_count"],
        "valid_count": out["output_count"],
        **out,
        "dataset": {
            "left": data.get("left") or [],
            "right": data.get("right") or [],
            "quarantine": data.get("quarantine", 0),
            "empty": data.get("empty", 0),
            "suggested_claims": data.get("suggested_claims") or [],
            "source": payload.get("dataset_source") if payload else "sample_data/reconcile_records.json",
        },
        "ledger": {
            "aliases": aliases,
            "revisions": ledger.revisions(),
            "operations": ledger.operations(),
            "conflicts": ledger.conflicts(),
        },
    }


def execute(payload: dict | None = None) -> dict:
    """无请求上下文的兼容入口：只用内置样例与传入别名。"""
    return core.run(payload or core.SAMPLE)


def claim(payload: dict, ledger: Ledger) -> dict:
    data = dataset(payload)
    return ledger.claim(payload, data)


def undo(alias_id: str, payload: dict, ledger: Ledger) -> dict:
    data = dataset(payload)
    return ledger.undo(alias_id, payload, data)


def split(alias_id: str, payload: dict, ledger: Ledger) -> dict:
    data = dataset(payload)
    parts = payload.get("parts") or []
    return ledger.split(alias_id, parts, payload, data)


def require_alias(alias_id: str, ledger: Ledger) -> dict:
    alias = next((item for item in ledger.aliases() if item["alias_id"] == alias_id), None)
    if not alias:
        raise DomainError("ALIAS_NOT_FOUND", "认领不存在")
    return alias
