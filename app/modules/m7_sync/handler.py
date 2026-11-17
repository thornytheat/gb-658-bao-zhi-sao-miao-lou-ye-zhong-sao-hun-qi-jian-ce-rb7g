from fastapi import APIRouter, Body, Query, Request
from fastapi.responses import HTMLResponse

from app.web import page

from . import service

router = APIRouter()

ROWS = [
    {"key": "ST-01 · 604", "status": "已入账", "value": "12"},
    {"key": "ST-02 · 118", "status": "待前序", "value": "1"},
    {"key": "ST-01 · 609", "status": "冲突挂起", "value": "1"},
]


@router.get("/sync", response_class=HTMLResponse)
def view(request: Request):
    return page(request, "m7.html", "离线同步", "/api/sync/batches", ROWS)


@router.post("/api/sync/batches")
def receive_batch(payload: dict = Body(...), batch_id: str | None = Query(None)):
    return service.receive(payload, batch_id)


@router.get("/api/sync/batches/{batch_id}")
def get_batch(batch_id: str):
    return service.read_batch(batch_id)


@router.get("/api/sync/conflicts")
def get_conflicts(status: str = Query("OPEN"), business_key: str | None = Query(None)):
    return service.list_conflicts(status, business_key)


@router.post("/api/sync/conflicts/{conflict_id}/resolve")
def resolve_conflict(conflict_id: str, payload: dict | None = Body(None)):
    return service.resolve(conflict_id, payload or {})


@router.post("/api/sync/reviews")
def post_review(payload: dict = Body(...)):
    return service.review(payload)


@router.post("/api/sync/recover")
def post_recover(payload: dict | None = Body(None)):
    return service.recover(payload or {})


@router.get("/api/sync/records/{business_key:path}")
def get_record(business_key: str):
    return service.read_record(business_key)


@router.get("/api/sync/revisions")
def get_revisions(
    station_id: str | None = Query(None),
    business_key: str | None = Query(None),
    status: str | None = Query(None),
):
    return service.list_revisions(station_id, business_key, status)


@router.get("/api/sync/scans")
def get_scans(station_id: str | None = Query(None), business_key: str | None = Query(None)):
    return service.list_scans(station_id, business_key)


@router.get("/api/sync/quarantine")
def get_quarantine(station_id: str | None = Query(None)):
    return service.list_quarantine(station_id)


@router.get("/api/sync/progress")
def get_progress(station_id: str | None = Query(None)):
    return service.progress(station_id)
