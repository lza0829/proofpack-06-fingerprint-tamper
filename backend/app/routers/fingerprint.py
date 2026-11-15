from typing import Any

from fastapi import APIRouter, Query

from app.services import fingerprint_service as svc

router = APIRouter(tags=["fingerprint"])


@router.get("/fingerprint/audits")
def list_audits(limit: int = Query(default=100, ge=1, le=500)):
    return svc.list_audits(limit=limit)


@router.get("/fingerprint/{pack_id}")
def get_report(pack_id: int):
    return svc.get_report(pack_id)


@router.post("/fingerprint/{pack_id}/verify")
def verify(pack_id: int) -> dict[str, Any]:
    return svc.verify(pack_id)


@router.post("/fingerprint/{pack_id}/rebuild")
def rebuild(pack_id: int) -> dict[str, Any]:
    return svc.rebuild(pack_id)
