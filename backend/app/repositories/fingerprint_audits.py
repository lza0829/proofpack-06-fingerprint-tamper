from __future__ import annotations

import sqlite3
from typing import Any


def insert(
    conn: sqlite3.Connection,
    *,
    pack_id: int,
    event: str,
    policy: str,
    stored_fingerprint: str | None,
    computed_fingerprint: str | None,
    detail: str,
    created_at: str,
) -> int:
    cur = conn.execute(
        """
        INSERT INTO fingerprint_audits(
            pack_id, event, policy, stored_fingerprint, computed_fingerprint, detail, created_at
        ) VALUES (?,?,?,?,?,?,?)
        """,
        (
            pack_id,
            event,
            policy,
            stored_fingerprint,
            computed_fingerprint,
            detail,
            created_at,
        ),
    )
    return int(cur.lastrowid)


def list_for_pack(
    conn: sqlite3.Connection,
    pack_id: int,
    limit: int = 100,
) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT id, pack_id, event, policy, stored_fingerprint, computed_fingerprint, detail, created_at
        FROM fingerprint_audits
        WHERE pack_id=?
        ORDER BY id
        LIMIT ?
        """,
        (pack_id, limit),
    ).fetchall()
    return [dict(r) for r in rows]


def list_all(conn: sqlite3.Connection, limit: int = 100) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT id, pack_id, event, policy, stored_fingerprint, computed_fingerprint, detail, created_at
        FROM fingerprint_audits
        ORDER BY id DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    return [dict(r) for r in rows]
