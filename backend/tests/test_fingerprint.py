from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

import pytest

# 测试用独立数据目录，必须在导入 app 之前设置
_TEST_DATA_DIR = Path(tempfile.mkdtemp(prefix="proofpack-test-"))
os.environ["DATA_DIR"] = str(_TEST_DATA_DIR)
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient  # noqa: E402

from app.db import connect  # noqa: E402
from app.engines.checksum import pack_fingerprint  # noqa: E402
from app.main import app  # noqa: E402
from app.seed import init_db  # noqa: E402


@pytest.fixture(autouse=True)
def clean_db():
    init_db()
    conn = connect()
    try:
        for table in (
            "release_decisions",
            "fingerprint_audits",
            "checklist_results",
            "pack_items",
            "evidence_packs",
            "sample_rows",
            "sample_batches",
        ):
            conn.execute(f"DELETE FROM {table}")
        conn.execute(
            "UPDATE settings SET value=? WHERE key=?",
            ("reject_only", "fingerprint_mismatch_policy"),
        )
        conn.commit()
    finally:
        conn.close()
    yield


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


def _make_pack(client: TestClient) -> dict:
    subject = client.get("/api/subjects").json()[0]
    batch = client.post(
        "/api/samples/run", json={"subject_id": subject["id"], "rate": 1.0}
    ).json()
    assert batch["sample_count"] > 1
    pack = client.post("/api/packs/build", json={"batch_id": batch["id"]}).json()
    result = client.post("/api/checklist/run", json={"pack_id": pack["id"]}).json()
    assert result["all_passed"] is True
    return pack


def _set_policy(value: str) -> None:
    conn = connect()
    try:
        conn.execute(
            "UPDATE settings SET value=? WHERE key='fingerprint_mismatch_policy'",
            (value,),
        )
        conn.commit()
    finally:
        conn.close()


def _tamper_item_summary(pack_id: int, key: str, field: str, value) -> None:
    conn = connect()
    try:
        row = conn.execute(
            "SELECT id, summary_json FROM pack_items WHERE pack_id=? AND business_key=?",
            (pack_id, key),
        ).fetchone()
        summary = json.loads(row["summary_json"])
        summary[field] = value
        conn.execute(
            "UPDATE pack_items SET summary_json=? WHERE id=?",
            (json.dumps(summary, ensure_ascii=False), row["id"]),
        )
        conn.commit()
    finally:
        conn.close()


def _decisions(pack_id: int) -> list[dict]:
    conn = connect()
    try:
        return [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM release_decisions WHERE pack_id=? ORDER BY id", (pack_id,)
            ).fetchall()
        ]
    finally:
        conn.close()


def _audits(pack_id: int) -> list[dict]:
    conn = connect()
    try:
        return [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM fingerprint_audits WHERE pack_id=? ORDER BY id", (pack_id,)
            ).fetchall()
        ]
    finally:
        conn.close()


def _pack_row(pack_id: int) -> dict:
    conn = connect()
    try:
        return dict(
            conn.execute("SELECT * FROM evidence_packs WHERE id=?", (pack_id,)).fetchone()
        )
    finally:
        conn.close()


def test_intact_pack_can_release(client):
    pack = _make_pack(client)
    report = client.get(f"/api/fingerprint/{pack['id']}").json()
    assert report["matched"] is True
    assert report["release_allowed"] is True

    resp = client.post(
        "/api/releases/decide", json={"pack_id": pack["id"], "decision": "pass"}
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["fingerprint_verified"] is True
    assert _pack_row(pack["id"])["status"] == "released"


@pytest.mark.parametrize("field,value", [("amount", 1.0), ("title", "被手改的摘要")])
def test_tampered_item_rejected_reject_only(client, field, value):
    pack = _make_pack(client)
    _tamper_item_summary(pack["id"], "SD-1001", field, value)

    resp = client.post(
        "/api/releases/decide", json={"pack_id": pack["id"], "decision": "pass"}
    )
    assert resp.status_code == 409, resp.text
    body = resp.json()["detail"]
    assert body["code"] == "fingerprint_mismatch"
    assert body["policy"] == "reject_only"
    assert body["disposition"] == "rejected"

    row = _pack_row(pack["id"])
    assert row["status"] == "fingerprint_mismatch"
    assert row["fingerprint_state"] == "mismatch"
    # 绝不写出放行通过
    assert _decisions(pack["id"]) == []
    # 重新跑清单也不得把失配态覆盖掉
    client.post("/api/checklist/run", json={"pack_id": pack["id"]})
    assert _pack_row(pack["id"])["status"] == "fingerprint_mismatch"
    # 留痕记录指纹不符原因
    audits = _audits(pack["id"])
    assert any(a["event"] == "mismatch" for a in audits)
    assert any(a["event"] == "repack_required" for a in audits)
    assert audits[0]["stored_fingerprint"] != audits[0]["computed_fingerprint"]


def test_item_added_or_removed_triggers_mismatch(client):
    pack = _make_pack(client)
    conn = connect()
    try:
        conn.execute(
            "DELETE FROM pack_items WHERE pack_id=? AND business_key=?",
            (pack["id"], "SD-1002"),
        )
        conn.commit()
    finally:
        conn.close()

    report = client.get(f"/api/fingerprint/{pack['id']}").json()
    assert report["matched"] is False
    assert report["actual_item_count"] == report["declared_item_count"] - 1

    resp = client.post(
        "/api/releases/decide", json={"pack_id": pack["id"], "decision": "pass"}
    )
    assert resp.status_code == 409


def test_reject_only_cannot_be_bypassed_by_restoring_items(client):
    pack = _make_pack(client)
    _tamper_item_summary(pack["id"], "SD-1001", "amount", 1.0)
    assert (
        client.post(
            "/api/releases/decide", json={"pack_id": pack["id"], "decision": "pass"}
        ).status_code
        == 409
    )

    # 改回使重算指纹与落库一致，但包已判失配，仅拒绝策略下仍不得放行
    _tamper_item_summary(pack["id"], "SD-1001", "amount", 12850.50)
    report = client.get(f"/api/fingerprint/{pack['id']}").json()
    assert report["matched"] is True
    assert report["locked"] is True
    assert report["release_allowed"] is False

    assert (
        client.post(
            "/api/releases/decide", json={"pack_id": pack["id"], "decision": "pass"}
        ).status_code
        == 409
    )
    assert _decisions(pack["id"]) == []


def test_reject_only_denies_local_rebuild(client):
    pack = _make_pack(client)
    _tamper_item_summary(pack["id"], "SD-1001", "amount", 1.0)
    resp = client.post(f"/api/fingerprint/{pack['id']}/rebuild")
    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"]["code"] == "rebuild_not_allowed"
    assert _pack_row(pack["id"])["status"] == "checked_ok"  # 未在核验前被改动


def test_rebuild_policy_rebuilds_and_requires_recheck(client):
    _set_policy("rebuild_and_recheck")
    pack = _make_pack(client)
    old_fp = pack["fingerprint"]
    _tamper_item_summary(pack["id"], "SD-1001", "amount", 1.0)

    resp = client.post(
        "/api/releases/decide", json={"pack_id": pack["id"], "decision": "pass"}
    )
    assert resp.status_code == 409, resp.text
    body = resp.json()["detail"]
    assert body["policy"] == "rebuild_and_recheck"
    assert body["disposition"] == "rebuilt"
    assert body["pack_status"] == "fingerprint_rebuilt"

    row = _pack_row(pack["id"])
    assert row["fingerprint"] != old_fp
    assert row["fingerprint_state"] == "ok"
    assert row["status"] == "fingerprint_rebuilt"
    # 旧清单作废
    assert client.get(f"/api/checklist?pack_id={pack['id']}").json() == []
    assert _decisions(pack["id"]) == []

    # 未复检即再申请放行 -> 拒绝
    resp = client.post(
        "/api/releases/decide", json={"pack_id": pack["id"], "decision": "pass"}
    )
    assert resp.status_code == 400
    assert "校验清单" in resp.json()["detail"]

    # 复检后放行：指纹按现行条目一致，清单通过
    check = client.post("/api/checklist/run", json={"pack_id": pack["id"]}).json()
    assert check["all_passed"] is True
    resp = client.post(
        "/api/releases/decide", json={"pack_id": pack["id"], "decision": "pass"}
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["fingerprint_verified"] is True

    events = [a["event"] for a in _audits(pack["id"])]
    assert "mismatch" in events and "rebuild" in events
    # 指纹按生成时同一算法重算
    conn = connect()
    try:
        items = [
            dict(r)
            for r in conn.execute(
                "SELECT business_key, summary_json FROM pack_items WHERE pack_id=?",
                (pack["id"],),
            ).fetchall()
        ]
    finally:
        conn.close()
    restored = [
        {"business_key": it["business_key"], **json.loads(it["summary_json"])}
        for it in items
    ]
    assert pack_fingerprint(restored) == _pack_row(pack["id"])["fingerprint"]


def test_rebuild_on_intact_pack_is_noop_rejection(client):
    _set_policy("rebuild_and_recheck")
    pack = _make_pack(client)
    resp = client.post(f"/api/fingerprint/{pack['id']}/rebuild")
    assert resp.status_code == 400
    assert "无需重建" in resp.json()["detail"]
    assert _pack_row(pack["id"])["fingerprint"] == pack["fingerprint"]


def test_verify_endpoint_records_and_reports(client):
    pack = _make_pack(client)
    ok = client.post(f"/api/fingerprint/{pack['id']}/verify").json()
    assert ok["matched"] is True and ok["release_allowed"] is True

    _tamper_item_summary(pack["id"], "SD-1003", "qty", 999)
    bad = client.post(f"/api/fingerprint/{pack['id']}/verify").json()
    assert bad["matched"] is False
    assert bad["release_allowed"] is False
    assert bad["reasons"]
    assert _pack_row(pack["id"])["status"] == "fingerprint_mismatch"
    assert client.get("/api/fingerprint/audits").json()


def test_reject_decision_still_requires_reason(client):
    pack = _make_pack(client)
    resp = client.post(
        "/api/releases/decide", json={"pack_id": pack["id"], "decision": "reject"}
    )
    assert resp.status_code == 400


def test_settings_policy_validation(client):
    resp = client.put(
        "/api/settings",
        json={"key": "fingerprint_mismatch_policy", "value": "rebuild_and_recheck"},
    )
    assert resp.status_code == 200
    assert resp.json()["fingerprint_mismatch_policy"] == "rebuild_and_recheck"

    resp = client.put(
        "/api/settings", json={"key": "fingerprint_mismatch_policy", "value": "bogus"}
    )
    assert resp.status_code == 400
