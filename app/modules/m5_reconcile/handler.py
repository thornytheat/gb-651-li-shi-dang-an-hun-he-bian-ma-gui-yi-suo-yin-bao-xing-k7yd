from __future__ import annotations

from fastapi import APIRouter, Body, Request
from fastapi.responses import JSONResponse

from . import core, service

router = APIRouter(tags=["M5-索引对账"])


@router.get("/reconcile")
def page(request: Request):
    ledger = service.ledger_for(request)
    service.seed(None, ledger)
    data = service.report(None, ledger)
    return request.app.state.templates.TemplateResponse(request, "m5_reconcile.html", {"title": "索引对账", "data": data})


@router.post("/api/reconcile")
def api(request: Request, payload: dict = Body(default={})):
    payload = payload or {}
    has_records = any(key in payload for key in ("left", "right", "index_ids", "normalized_ids", "dataset"))
    if not has_records:
        data = core.run(payload)
        data["ledger"] = {
            "note": "未传记录集合，使用内置样例的只读结果；台账操作请使用 /api/reconcile/* 并传记录集合。",
        }
        return data
    ledger = service.ledger_for(request)
    service.seed(payload, ledger)
    return service.report(payload, ledger)


@router.get("/api/reconcile/ledger")
def ledger_view(request: Request):
    ledger = service.ledger_for(request)
    return {
        "aliases": ledger.aliases(),
        "revisions": ledger.revisions(),
        "operations": ledger.operations(),
        "conflicts": ledger.conflicts(),
    }


@router.post("/api/reconcile/claim")
def claim(request: Request, payload: dict = Body(...)):
    ledger = service.ledger_for(request)
    return service.claim(payload, ledger)


@router.post("/api/reconcile/claims/{alias_id}/undo")
def undo(alias_id: str, request: Request, payload: dict = Body(default={})):
    ledger = service.ledger_for(request)
    return service.undo(alias_id, payload or {}, ledger)


@router.post("/api/reconcile/claims/{alias_id}/split")
def split(alias_id: str, request: Request, payload: dict = Body(default={})):
    ledger = service.ledger_for(request)
    if not payload.get("parts"):
        return JSONResponse(status_code=422, content={"error": "INVALID_SPLIT", "message": "拆分至少需要两部分"})
    return service.split(alias_id, payload, ledger)


@router.get("/api/reconcile/claims/{alias_id}/evidence")
def evidence(alias_id: str, request: Request):
    ledger = service.ledger_for(request)
    service.require_alias(alias_id, ledger)
    return {"alias_id": alias_id, "evidence": ledger.revisions(alias_id)}
