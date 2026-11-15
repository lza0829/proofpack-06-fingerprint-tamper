from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Any

from fastapi import HTTPException

from app.db import connect
from app.engines.checksum import items_from_pack_rows, pack_fingerprint
from app.repositories import checklist as check_repo
from app.repositories import fingerprint_audits as audit_repo
from app.repositories import packs as pack_repo
from app.repositories import settings as settings_repo

# 指纹失配处置策略（互斥，取自 settings.fingerprint_mismatch_policy）
POLICY_REJECT_ONLY = "reject_only"          # 仅拒绝，保持指纹失配态，须显式重新打包
POLICY_REBUILD_AND_RECHECK = "rebuild_and_recheck"  # 允许按现行条目重建，重建后强制复检
DEFAULT_POLICY = POLICY_REJECT_ONLY
POLICIES = (POLICY_REJECT_ONLY, POLICY_REBUILD_AND_RECHECK)

# 包状态
PACK_STATUS_MISMATCH = "fingerprint_mismatch"
PACK_STATUS_REBUILT = "fingerprint_rebuilt"


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def get_policy(conn: sqlite3.Connection) -> str:
    value = settings_repo.get_setting(
        conn, "fingerprint_mismatch_policy", DEFAULT_POLICY
    )
    if value not in POLICIES:
        # 配置异常时按最严策略处置，绝不因配置错误而放宽
        return DEFAULT_POLICY
    return str(value)


def evaluate(conn: sqlite3.Connection, pack: dict[str, Any]) -> dict[str, Any]:
    """按现行包内条目重算指纹，与落库指纹比对。"""
    rows = pack_repo.list_items(conn, pack["id"])
    computed = pack_fingerprint(items_from_pack_rows(rows))
    stored = pack.get("fingerprint")
    declared_count = int(pack.get("item_count") or 0)
    actual_count = len(rows)
    matched = computed == stored and actual_count == declared_count

    reasons: list[str] = []
    if computed != stored:
        reasons.append("包内条目重算指纹与落库指纹不一致")
    if actual_count != declared_count:
        reasons.append(f"包内条目数 {actual_count} 与落库条数 {declared_count} 不一致")

    return {
        "pack_id": pack["id"],
        "pack_no": pack.get("pack_no"),
        "stored_fingerprint": stored,
        "computed_fingerprint": computed,
        "declared_item_count": declared_count,
        "actual_item_count": actual_count,
        "matched": matched,
        "fingerprint_state": pack.get("fingerprint_state") or "ok",
        "fingerprint_updated_at": pack.get("fingerprint_updated_at"),
        "reasons": reasons,
    }


def _is_locked(pack: dict[str, Any], policy: str) -> bool:
    """仅拒绝策略下，已判失配的包保持失配态，即使条目被改回也不得放行。"""
    return (
        policy == POLICY_REJECT_ONLY
        and (pack.get("fingerprint_state") or "ok") == "mismatch"
    )


def _detail(ev: dict[str, Any]) -> str:
    return "；".join(ev["reasons"]) if ev["reasons"] else "指纹一致"


def _record(
    conn: sqlite3.Connection,
    pack_id: int,
    *,
    event: str,
    policy: str,
    ev: dict[str, Any],
    detail: str | None = None,
    created_at: str | None = None,
) -> int:
    return audit_repo.insert(
        conn,
        pack_id=pack_id,
        event=event,
        policy=policy,
        stored_fingerprint=ev.get("stored_fingerprint"),
        computed_fingerprint=ev.get("computed_fingerprint"),
        detail=detail if detail is not None else _detail(ev),
        created_at=created_at or _now(),
    )


def verify(pack_id: int) -> dict[str, Any]:
    """显式核验：重算指纹并与落库值比对，留痕；不重建、不放行。"""
    conn = connect()
    try:
        pack = pack_repo.get_pack(conn, pack_id)
        if not pack:
            raise HTTPException(status_code=404, detail="证据包不存在")
        policy = get_policy(conn)
        ev = evaluate(conn, pack)

        if ev["matched"]:
            if _is_locked(pack, policy):
                # 仅拒绝策略下已判失配的包保持失配态，须重新打包
                _record(
                    conn,
                    pack_id,
                    event="repack_required",
                    policy=policy,
                    ev=ev,
                    detail="包仍处于指纹失配态（仅拒绝策略），须显式重新打包",
                )
                conn.commit()
                ev["locked"] = True
                ev["release_allowed"] = False
                ev["policy"] = policy
                return ev
            if pack.get("fingerprint_state") != "ok":
                pack_repo.set_fingerprint_state(
                    conn, pack_id, "ok", ev["fingerprint_updated_at"] or _now()
                )
                pack["fingerprint_state"] = "ok"
            _record(conn, pack_id, event="verify", policy=policy, ev=ev)
            conn.commit()
            ev["fingerprint_state"] = "ok"
            ev["locked"] = False
            ev["release_allowed"] = True
            ev["policy"] = policy
            return ev

        outcome = _handle_mismatch(
            conn,
            pack=pack,
            ev=ev,
            policy=policy,
            source="verify",
            record_event="mismatch",
        )
        conn.commit()
        ev["fingerprint_state"] = (
            "ok" if outcome["disposition"] == "rebuilt" else "mismatch"
        )
        ev["pack_status"] = outcome["pack_status"]
        ev["locked"] = policy == POLICY_REJECT_ONLY
        # 重建后旧清单已作废，必须复检后才可放行；仅拒绝则须重新打包
        ev["release_allowed"] = False
        ev["policy"] = policy
        return ev
    finally:
        conn.close()


def _handle_mismatch(
    conn: sqlite3.Connection,
    *,
    pack: dict[str, Any],
    ev: dict[str, Any],
    policy: str,
    source: str,
    record_event: str,
) -> dict[str, Any]:
    """按所选策略处置指纹失配；返回处置结果（不提交事务）。

    - reject_only：仅拒绝，包保持指纹失配态；旧清单作废；须显式重新打包。
    - rebuild_and_recheck：按现行条目重建指纹并作废旧清单；包须重新复检。
    """
    now = _now()
    detail = _detail(ev)
    _record(
        conn,
        pack["id"],
        event=record_event,
        policy=policy,
        ev=ev,
        detail=f"指纹失配（{source}）：{detail}",
        created_at=now,
    )

    # 旧清单与旧通过态一律作废，不得沿用
    check_repo.clear_for_pack(conn, pack["id"])

    if policy == POLICY_REBUILD_AND_RECHECK:
        rows = pack_repo.list_items(conn, pack["id"])
        computed = ev["computed_fingerprint"]
        pack_repo.replace_fingerprint(
            conn, pack["id"], computed, len(rows), now
        )
        pack_repo.update_status(conn, pack["id"], PACK_STATUS_REBUILT)
        _record(
            conn,
            pack["id"],
            event="rebuild",
            policy=policy,
            ev=ev,
            detail=(
                f"已按现行 {len(rows)} 条条目重建指纹：旧 {ev['stored_fingerprint']} → "
                f"新 {computed}；旧清单已作废，须重新跑校验清单后方可再申请放行"
            ),
            created_at=now,
        )
        return {
            "disposition": "rebuilt",
            "rebuild_required_recheck": True,
            "pack_status": PACK_STATUS_REBUILT,
            "old_fingerprint": ev["stored_fingerprint"],
            "new_fingerprint": computed,
            "detail": detail,
        }

    pack_repo.set_fingerprint_state(conn, pack["id"], "mismatch", now)
    pack_repo.update_status(conn, pack["id"], PACK_STATUS_MISMATCH)
    _record(
        conn,
        pack["id"],
        event="repack_required",
        policy=policy,
        ev=ev,
        detail="仅拒绝放行，包保持指纹失配态；须显式重新打包生成新指纹与新清单后方可再申请",
        created_at=now,
    )
    return {
        "disposition": "rejected",
        "pack_status": PACK_STATUS_MISMATCH,
        "detail": detail,
    }


def guard_release(
    conn: sqlite3.Connection,
    pack: dict[str, Any],
) -> dict[str, Any]:
    """放行前的指纹闸门：一致才返回；失配则按策略处置并拒绝放行。

    一致时把状态修正留在当前事务中，由放行决策统一提交；
    失配时处置与留痕先行提交，再拒绝放行，绝不写出放行通过记录。
    """
    policy = get_policy(conn)
    ev = evaluate(conn, pack)
    if ev["matched"] and _is_locked(pack, policy):
        conn.commit()
        raise HTTPException(
            status_code=409,
            detail={
                "code": "fingerprint_mismatch",
                "message": "包处于指纹失配态，拒绝放行；须显式重新打包生成新指纹与新清单",
                "pack_id": pack["id"],
                "stored_fingerprint": ev["stored_fingerprint"],
                "computed_fingerprint": ev["computed_fingerprint"],
                "reasons": ev["reasons"] or ["包已被判指纹失配"],
                "policy": policy,
                "disposition": "rejected",
                "pack_status": PACK_STATUS_MISMATCH,
            },
        )
    if ev["matched"]:
        if pack.get("fingerprint_state") != "ok":
            pack_repo.set_fingerprint_state(conn, pack["id"], "ok", _now())
        ev["policy"] = policy
        return ev

    outcome = _handle_mismatch(
        conn,
        pack=pack,
        ev=ev,
        policy=policy,
        source="release",
        record_event="mismatch",
    )
    # 失配处置（留痕 / 作废旧清单 / 按策略重建）须先落库，再拒绝放行，
    # 避免异常中断导致处置与留痕丢失、却留下可继续放行的状态。
    conn.commit()
    raise HTTPException(
        status_code=409,
        detail={
            "code": "fingerprint_mismatch",
            "message": "包指纹失配，拒绝放行",
            "pack_id": pack["id"],
            "stored_fingerprint": ev["stored_fingerprint"],
            "computed_fingerprint": ev["computed_fingerprint"],
            "reasons": ev["reasons"],
            "policy": policy,
            "disposition": outcome["disposition"],
            "pack_status": outcome["pack_status"],
        },
    )


def rebuild(pack_id: int) -> dict[str, Any]:
    """显式按现行条目重建指纹；仅在 rebuild_and_recheck 策略下允许。"""
    conn = connect()
    try:
        pack = pack_repo.get_pack(conn, pack_id)
        if not pack:
            raise HTTPException(status_code=404, detail="证据包不存在")
        policy = get_policy(conn)
        ev = evaluate(conn, pack)

        if policy != POLICY_REBUILD_AND_RECHECK:
            if ev["matched"]:
                raise HTTPException(
                    status_code=400,
                    detail="包指纹一致，无需重建",
                )
            _record(
                conn,
                pack_id,
                event="rebuild_denied",
                policy=policy,
                ev=ev,
                detail="当前策略为仅拒绝：须重新打包，不得就地重建指纹",
            )
            conn.commit()
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "rebuild_not_allowed",
                    "message": "当前策略为仅拒绝，须显式重新打包生成新指纹与新清单",
                    "policy": policy,
                    "reasons": ev["reasons"],
                },
            )

        if ev["matched"]:
            raise HTTPException(status_code=400, detail="包指纹一致，无需重建")

        outcome = _handle_mismatch(
            conn,
            pack=pack,
            ev=ev,
            policy=policy,
            source="rebuild",
            record_event="mismatch",
        )
        conn.commit()
        return {
            "pack_id": pack_id,
            "pack_no": pack.get("pack_no"),
            "policy": policy,
            "fingerprint_state": (
                "ok" if outcome["disposition"] == "rebuilt" else "mismatch"
            ),
            "release_allowed": False,
            "recheck_required": True,
            **outcome,
        }
    finally:
        conn.close()


def get_report(pack_id: int) -> dict[str, Any]:
    """核验视图：现行指纹比对结果 + 指纹留痕。只读，不落库。"""
    conn = connect()
    try:
        pack = pack_repo.get_pack(conn, pack_id)
        if not pack:
            raise HTTPException(status_code=404, detail="证据包不存在")
        ev = evaluate(conn, pack)
        policy = get_policy(conn)
        ev["policy"] = policy
        ev["locked"] = _is_locked(pack, policy)
        ev["release_allowed"] = ev["matched"] and not ev["locked"]
        ev["pack_status"] = pack.get("status")
        ev["audits"] = audit_repo.list_for_pack(conn, pack_id)
        return ev
    finally:
        conn.close()


def list_audits(limit: int = 100) -> list[dict[str, Any]]:
    conn = connect()
    try:
        rows = audit_repo.list_all(conn, limit=limit)
        packs = {p["id"]: p for p in pack_repo.list_packs(conn, limit=500)}
        for r in rows:
            p = packs.get(r["pack_id"])
            r["pack_no"] = p["pack_no"] if p else None
            r["pack_status"] = p["status"] if p else None
        return rows
    finally:
        conn.close()
