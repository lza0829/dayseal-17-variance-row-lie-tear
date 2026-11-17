from __future__ import annotations
import sqlite3
from typing import Any

from app.variance_policy import FINAL_DISPOSITIONS

_ZERO = 1e-9

_BATCH_COLUMNS = """
  id, batch_no, business_day, system_total, declared_cash, variance,
  line_count, status, sealed_at, note
"""


def is_sealed(conn: sqlite3.Connection, business_day: str) -> bool:
    """日锁定状态：唯一来源是封账批次中是否存在 sealed 批次。"""
    return day_state(conn, business_day)["sealed"]


def sealed_batch(conn: sqlite3.Connection, business_day: str) -> dict[str, Any] | None:
    row = conn.execute(
        f"SELECT {_BATCH_COLUMNS} FROM close_batches WHERE business_day=? AND status='sealed' ORDER BY id DESC LIMIT 1",
        (business_day,),
    ).fetchone()
    return dict(row) if row else None


def variance_for_batch(conn: sqlite3.Connection, batch_id: int) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT * FROM variance_rows WHERE batch_id=? ORDER BY id DESC LIMIT 1",
        (batch_id,),
    ).fetchone()
    return dict(row) if row else None


def day_state(conn: sqlite3.Connection, business_day: str) -> dict[str, Any]:
    """营业日的封账/锁日/处置状态，全部由批次与长短款行同源推导。"""
    batch = sealed_batch(conn, business_day)
    if not batch:
        return {
            "sealed": False,
            "locked": False,
            "batch": None,
            "disposition": None,
            "variance_row": None,
        }
    row = variance_for_batch(conn, int(batch["id"]))
    return {
        "sealed": True,
        "locked": True,
        "batch": batch,
        "disposition": row["disposition"] if row else None,
        "variance_row": row,
    }


def next_batch_no(conn: sqlite3.Connection) -> str:
    n = conn.execute("SELECT COALESCE(MAX(id),0) c FROM close_batches").fetchone()["c"] + 1
    return f"CB-{n:04d}"


def create_batch(conn: sqlite3.Connection, **fields: Any) -> int:
    """写入封账批次。封账流程先以 draft 建批次，处置记录落库后再置 sealed。"""
    cur = conn.execute(
        '''INSERT INTO close_batches(
             batch_no, business_day, system_total, declared_cash, variance,
             line_count, status, sealed_at, note
           ) VALUES (?,?,?,?,?,?,?,?,?)''',
        (
            fields["batch_no"],
            fields["business_day"],
            fields["system_total"],
            fields["declared_cash"],
            fields["variance"],
            fields["line_count"],
            fields["status"],
            fields["sealed_at"],
            fields.get("note"),
        ),
    )
    return int(cur.lastrowid)


def seal_batch(conn: sqlite3.Connection, batch_id: int) -> None:
    """置 sealed；触发器会校验该批次已有最终处置的长短款行。"""
    conn.execute("UPDATE close_batches SET status='sealed' WHERE id=? AND status='draft'", (batch_id,))


def add_variance(conn: sqlite3.Connection, **fields: Any) -> None:
    """写入处置记录（长短款行）。disposition 只允许最终态。"""
    disposition = fields["disposition"]
    if disposition not in FINAL_DISPOSITIONS:
        raise ValueError(f"非法处置状态：{disposition}")
    conn.execute(
        '''INSERT INTO variance_rows(batch_id, business_day, variance, disposition, note, created_at)
           VALUES (?,?,?,?,?,?)''',
        (
            fields["batch_id"],
            fields["business_day"],
            fields["variance"],
            disposition,
            fields.get("note"),
            fields["created_at"],
        ),
    )


def list_batches(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """批次与处置记录同源返回；不存在长短款行的批次会暴露为 disposition=NULL。"""
    rows = conn.execute(
        f'''
        SELECT b.{', b.'.join(c.strip() for c in _BATCH_COLUMNS.split(','))},
               v.disposition AS disposition, v.note AS disposition_note,
               v.variance AS variance_row_amount
        FROM close_batches b
        LEFT JOIN variance_rows v ON v.batch_id = b.id
        ORDER BY b.id DESC
        '''
    ).fetchall()
    return [dict(r) for r in rows]


def pending_days(conn: sqlite3.Connection) -> list[str]:
    """存在未处置差额的营业日。

    本策略禁止 pending，因此这里只会返回历史脏数据（例如旧策略遗留），
    用于日锁定审计与运维处置，不会由正常封账流程产生。
    """
    placeholders = ",".join("?" for _ in FINAL_DISPOSITIONS)
    return [
        r["business_day"]
        for r in conn.execute(
            f"SELECT DISTINCT business_day FROM variance_rows WHERE disposition NOT IN ({placeholders}) ORDER BY business_day",
            tuple(FINAL_DISPOSITIONS),
        ).fetchall()
    ]


def list_variances(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    return [dict(r) for r in conn.execute("SELECT * FROM variance_rows ORDER BY id DESC").fetchall()]


def day_pending_count(conn: sqlite3.Connection, business_day: str) -> int:
    """该营业日未终态（pending 等）长短款行数；>0 时禁止封账/重封。"""
    placeholders = ",".join("?" for _ in FINAL_DISPOSITIONS)
    row = conn.execute(
        f"SELECT COUNT(*) c FROM variance_rows WHERE business_day=? AND disposition NOT IN ({placeholders})",
        (business_day, *FINAL_DISPOSITIONS),
    ).fetchone()
    return int(row["c"])


def open_variance_count(conn: sqlite3.Connection) -> int:
    """总览未决计数：非终态处置行 + 已 sealed 却无终态处置行的批次。

    「已 sealed 无长短款行」与「pending 仍当已清」都计入未决，
    只有两者同时归零，总览才允许显示 0。
    """
    placeholders = ",".join("?" for _ in FINAL_DISPOSITIONS)
    final = tuple(FINAL_DISPOSITIONS)
    non_final = conn.execute(
        f"SELECT COUNT(*) c FROM variance_rows WHERE disposition NOT IN ({placeholders})",
        final,
    ).fetchone()["c"]
    sealed_without = conn.execute(
        f'''
        SELECT COUNT(*) c FROM close_batches b
        WHERE b.status='sealed'
          AND NOT EXISTS (
            SELECT 1 FROM variance_rows v
            WHERE v.batch_id=b.id AND v.disposition IN ({placeholders})
          )
        ''',
        final,
    ).fetchone()["c"]
    return int(non_final) + int(sealed_without)


def update_variance_row(
    conn: sqlite3.Connection, row_id: int, *, variance: float, disposition: str, note: str | None
) -> None:
    """以批次快照为准回写长短款行（金额与 disposition 同源）。"""
    conn.execute(
        "UPDATE variance_rows SET variance=?, disposition=?, note=? WHERE id=?",
        (variance, disposition, note, row_id),
    )


def delete_variance_row(conn: sqlite3.Connection, row_id: int) -> None:
    conn.execute("DELETE FROM variance_rows WHERE id=?", (row_id,))


def set_batch_status(conn: sqlite3.Connection, batch_id: int, status: str) -> None:
    conn.execute("UPDATE close_batches SET status=? WHERE id=?", (status, batch_id))


def integrity_violations(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """自检同源约束：用于接口暴露与提交前断言。"""
    violations: list[dict[str, Any]] = []
    placeholders = ",".join("?" for _ in FINAL_DISPOSITIONS)
    final = tuple(FINAL_DISPOSITIONS)

    for row in conn.execute(
        f'''
        SELECT b.id, b.business_day, b.batch_no
        FROM close_batches b
        WHERE b.status='sealed'
          AND NOT EXISTS (
            SELECT 1 FROM variance_rows v
            WHERE v.batch_id=b.id AND v.disposition IN ({placeholders})
          )
        ''',
        final,
    ).fetchall():
        violations.append(
            {
                "code": "sealed_without_variance",
                "business_day": row["business_day"],
                "batch_id": row["id"],
                "detail": f"{row['batch_no']} 已 sealed 却无最终处置的长短款行",
            }
        )

    for row in conn.execute(
        f"SELECT * FROM variance_rows WHERE disposition NOT IN ({placeholders})",
        final,
    ).fetchall():
        violations.append(
            {
                "code": "non_final_disposition",
                "business_day": row["business_day"],
                "batch_id": row["batch_id"],
                "detail": f"长短款行处置状态为 {row['disposition']}，全局策略禁止未处置态",
            }
        )

    for row in conn.execute(
        f'''
        SELECT v.* FROM variance_rows v
        JOIN close_batches b ON b.id=v.batch_id
        WHERE b.status='sealed' AND (
          (ABS(v.variance) < ? AND v.disposition <> 'balanced')
          OR (v.variance < -? AND v.disposition <> 'loss')
          OR (v.variance > ? AND v.disposition <> 'gain')
        )
        ''',
        (_ZERO, _ZERO, _ZERO),
    ).fetchall():
        violations.append(
            {
                "code": "disposition_variance_mismatch",
                "business_day": row["business_day"],
                "batch_id": row["batch_id"],
                "detail": f"差额 {row['variance']} 与处置 {row['disposition']} 方向不符",
            }
        )

    for row in conn.execute(
        '''
        SELECT v.id, v.business_day, v.batch_id, v.variance AS row_variance, b.variance AS batch_variance
        FROM variance_rows v
        JOIN close_batches b ON b.id=v.batch_id
        WHERE ABS(v.variance - b.variance) > ?
        ''',
        (_ZERO,),
    ).fetchall():
        violations.append(
            {
                "code": "variance_amount_mismatch",
                "business_day": row["business_day"],
                "batch_id": row["batch_id"],
                "detail": (
                    f"长短款行金额 {row['row_variance']} 与批次差额 {row['batch_variance']} 不同源"
                ),
            }
        )

    for row in conn.execute(
        '''
        SELECT v.id, v.business_day, v.batch_id
        FROM variance_rows v
        LEFT JOIN close_batches b ON b.id=v.batch_id
        WHERE b.id IS NULL
        '''
    ).fetchall():
        violations.append(
            {
                "code": "orphan_variance",
                "business_day": row["business_day"],
                "batch_id": row["batch_id"],
                "detail": f"长短款行 {row['id']} 无对应封账批次",
            }
        )

    for row in conn.execute(
        '''
        SELECT business_day, COUNT(*) c
        FROM close_batches
        WHERE status='sealed'
        GROUP BY business_day
        HAVING COUNT(*) > 1
        '''
    ).fetchall():
        violations.append(
            {
                "code": "multiple_sealed_batches",
                "business_day": row["business_day"],
                "batch_id": None,
                "detail": f"营业日存在 {row['c']} 个 sealed 批次",
            }
        )

    return violations
