from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.db import connect
from app.repositories import settings as settings_repo
from app.services import fingerprint_service
from app.services import release_service as svc

router = APIRouter(tags=["settings"])


class SettingBody(BaseModel):
    key: str
    value: str


@router.get("/settings")
def list_settings():
    return svc.list_settings()


@router.put("/settings")
def update_setting(body: SettingBody):
    if body.key != "fingerprint_mismatch_policy":
        raise HTTPException(status_code=400, detail="该配置项不支持在线修改")
    if body.value not in fingerprint_service.POLICIES:
        raise HTTPException(
            status_code=400,
            detail="指纹失配处置须为 reject_only 或 rebuild_and_recheck",
        )
    conn = connect()
    try:
        settings_repo.set_setting(conn, body.key, body.value)
        conn.commit()
        return settings_repo.list_settings(conn)
    finally:
        conn.close()
