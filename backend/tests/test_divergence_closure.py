"""分叉夹具互证：「有差无行」「行额≠批差」「pending 仍解封」。

每个夹具分两步互证：
  1. 以「老库」（无 CHECK/触发器的遗留表结构）埋入分叉数据，证明
     自检、总览未决计数、封账/重封闸都能识别并拦截；
  2. 调用 POST /api/close/repair（以批次合计与实盘快照为准），证明
     长短款行与 disposition 被同源重算/补齐，自检归零、未决归零。
"""
from __future__ import annotations
import os
import sqlite3
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# 隔离数据目录，必须在 import app.* 之前设置。
os.environ["DAYSEAL_DATA"] = tempfile.mkdtemp(prefix="dayseal-tear-")

from fastapi.testclient import TestClient  # noqa: E402
from app.main import app  # noqa: E402
from app.config import DB_PATH  # noqa: E402
from app.seed import init_db  # noqa: E402

DAY = "2026-11-15"
SYSTEM_TOTAL = 88.0

# 遗留库表结构：没有 CHECK、没有触发器——分叉数据只能这样产生。
_LEGACY_SCHEMA = """
CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE tills (
  id INTEGER PRIMARY KEY, till_code TEXT NOT NULL UNIQUE, title TEXT NOT NULL
);
CREATE TABLE cash_lines (
  id INTEGER PRIMARY KEY, line_no TEXT NOT NULL UNIQUE, till_code TEXT NOT NULL,
  business_day TEXT NOT NULL, occurred_at TEXT NOT NULL, amount REAL NOT NULL,
  pay_method TEXT NOT NULL, note TEXT, created_at TEXT NOT NULL
);
CREATE TABLE close_batches (
  id INTEGER PRIMARY KEY, batch_no TEXT NOT NULL UNIQUE, business_day TEXT NOT NULL,
  system_total REAL NOT NULL, declared_cash REAL NOT NULL, variance REAL NOT NULL,
  line_count INTEGER NOT NULL, status TEXT NOT NULL, sealed_at TEXT NOT NULL, note TEXT
);
CREATE TABLE variance_rows (
  id INTEGER PRIMARY KEY, batch_id INTEGER NOT NULL, business_day TEXT NOT NULL,
  variance REAL NOT NULL, disposition TEXT NOT NULL, note TEXT, created_at TEXT NOT NULL
);
"""


def _wipe_db() -> None:
    for suffix in ("", "-wal", "-shm"):
        p = Path(str(DB_PATH) + suffix)
        if p.exists():
            p.unlink()


def _plant_legacy(*, batches: list[tuple], variances: list[tuple]) -> None:
    """造一座埋了分叉数据的老库，再补上现行守卫（触发器/索引）。"""
    _wipe_db()
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.executescript(_LEGACY_SCHEMA)
        conn.execute("INSERT INTO settings(key, value) VALUES ('business_day', ?)", ("2026-11-16",))
        conn.execute("INSERT INTO tills(till_code, title) VALUES ('TILL-A', '一号收银')")
        conn.execute(
            """INSERT INTO cash_lines(line_no, till_code, business_day, occurred_at, amount, pay_method, note, created_at)
               VALUES ('CL-0001','TILL-A',?,'2026-11-15T18:10:00Z',88.0,'现金',NULL,'2026-11-15T18:10:00Z')""",
            (DAY,),
        )
        for b in batches:
            conn.execute(
                """INSERT INTO close_batches(id, batch_no, business_day, system_total, declared_cash,
                     variance, line_count, status, sealed_at, note)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                b,
            )
        for v in variances:
            conn.execute(
                """INSERT INTO variance_rows(batch_id, business_day, variance, disposition, note, created_at)
                   VALUES (?,?,?,?,?,?)""",
                v,
            )
        conn.commit()
    finally:
        conn.close()
    init_db()  # 老库加上现行守卫；脏数据保持原样，等自检暴露


@pytest.fixture()
def client():
    with TestClient(app) as c:
        yield c


def _integrity(client) -> dict:
    return client.get("/api/close/integrity").json()


def _codes(body: dict) -> set[str]:
    return {v["code"] for v in body["violations"]}


def _dash(client) -> dict:
    return client.get("/api/dashboard").json()


def _variances(client) -> list[dict]:
    return client.get("/api/close/variances").json()


def _repair(client) -> dict:
    r = client.post("/api/close/repair")
    assert r.status_code == 200, r.text
    return r.json()


# ---------------------------------------------------------------- 夹具一：有差无行

def plant_sealed_without_row():
    """已 sealed、批差 -38，却查无长短款行。"""
    _plant_legacy(
        batches=[(1, "CB-0001", DAY, 88.0, 50.0, -38.0, 1, "sealed", "2026-11-15T23:00:00Z", None)],
        variances=[],
    )


def test_fixture_sealed_without_row_is_detected_and_gated(client):
    plant_sealed_without_row()

    body = _integrity(client)
    assert body["ok"] is False
    assert "sealed_without_variance" in _codes(body)

    dash = _dash(client)
    assert dash["open_variance_count"] >= 1  # 总览未决计数不得当它已清
    assert dash["integrity_violation_count"] >= 1

    # 重封闸：已 sealed 不得重封；封后流水冻结
    r = client.post("/api/close/seal", json={"business_day": DAY, "declared_cash": 50.0, "disposition": "loss"})
    assert r.status_code == 409
    r = client.post("/api/lines", json={"business_day": DAY, "till_code": "TILL-A", "amount": 1})
    assert r.status_code == 409


def test_fixture_sealed_without_row_is_repaired_from_batch_snapshot(client):
    plant_sealed_without_row()

    result = _repair(client)
    assert result["ok"] is True
    assert [a["code"] for a in result["actions"]] == ["backfill_variance_row"]

    # 互证：补齐的行金额与批差同源、disposition 由差额推导
    rows = _variances(client)
    assert len(rows) == 1
    assert rows[0]["variance"] == -38.0
    assert rows[0]["disposition"] == "loss"
    assert rows[0]["batch_id"] == 1

    after = _integrity(client)
    assert after["ok"] is True and after["violations"] == []
    dash = _dash(client)
    assert dash["open_variance_count"] == 0
    assert dash["integrity_violation_count"] == 0

    preview = client.get(f"/api/close/preview?business_day={DAY}").json()
    assert preview["sealed"] is True and preview["disposition"] == "loss"
    assert preview["variance_row"]["variance"] == -38.0

    # 修复幂等：再修一次无任何动作
    assert _repair(client)["repaired_count"] == 0


# ---------------------------------------------------------------- 夹具二：行额≠批差

def plant_amount_mismatch():
    """已 sealed、批差 -38，认损行却写 -10（金额不同源）。"""
    _plant_legacy(
        batches=[(1, "CB-0001", DAY, 88.0, 50.0, -38.0, 1, "sealed", "2026-11-15T23:00:00Z", None)],
        variances=[(1, DAY, -10.0, "loss", "手工登记", "2026-11-15T23:00:01Z")],
    )


def test_fixture_amount_mismatch_is_detected(client):
    plant_amount_mismatch()
    body = _integrity(client)
    assert body["ok"] is False
    assert "variance_amount_mismatch" in _codes(body)
    assert _dash(client)["integrity_violation_count"] >= 1


def test_fixture_amount_mismatch_is_rewritten_to_batch_amount(client):
    plant_amount_mismatch()

    result = _repair(client)
    assert [a["code"] for a in result["actions"]] == ["rewrite_variance_row"]

    rows = _variances(client)
    assert len(rows) == 1
    assert rows[0]["variance"] == -38.0  # 与批差同源
    assert rows[0]["disposition"] == "loss"
    assert rows[0]["note"] == "手工登记"  # 说明文字保留，状态字段被纠正
    assert _integrity(client)["ok"] is True


def test_fixture_direction_lie_is_detected_and_rewritten(client):
    """批差 -38（短款），行却认溢 gain——方向撒谎。"""
    _plant_legacy(
        batches=[(1, "CB-0001", DAY, 88.0, 50.0, -38.0, 1, "sealed", "2026-11-15T23:00:00Z", None)],
        variances=[(1, DAY, -38.0, "gain", None, "2026-11-15T23:00:01Z")],
    )
    body = _integrity(client)
    assert "disposition_variance_mismatch" in _codes(body)

    _repair(client)
    row = _variances(client)[0]
    assert row["disposition"] == "loss" and row["variance"] == -38.0
    assert _integrity(client)["ok"] is True


# ---------------------------------------------------------------- 夹具三：pending 仍解封/重封

def plant_pending_on_sealed():
    """已 sealed 批次挂着 pending 行：未处置却冒充已封。"""
    _plant_legacy(
        batches=[(1, "CB-0001", DAY, 88.0, 50.0, -38.0, 1, "sealed", "2026-11-15T23:00:00Z", None)],
        variances=[(1, DAY, -38.0, "pending", "回头再处理", "2026-11-15T23:00:01Z")],
    )


def test_fixture_pending_counts_as_open_and_blocks_reseal(client):
    plant_pending_on_sealed()

    body = _integrity(client)
    assert body["ok"] is False
    assert "non_final_disposition" in _codes(body)
    assert body["pending_days"] == [DAY]

    dash = _dash(client)
    assert dash["open_variance_count"] >= 1  # pending 不得当作已清

    # 重封闸：pending 未清，任何封账/重封都不放行
    r = client.post("/api/close/seal", json={"business_day": DAY, "declared_cash": 50.0, "disposition": "loss"})
    assert r.status_code == 409


def test_fixture_pending_is_resolved_by_repair_not_by_note(client):
    plant_pending_on_sealed()

    result = _repair(client)
    assert [a["code"] for a in result["actions"]] == ["rewrite_variance_row"]

    row = _variances(client)[0]
    assert row["disposition"] == "loss"  # 由批次差额改判终态，不是靠备注
    assert row["variance"] == -38.0

    after = _integrity(client)
    assert after["ok"] is True
    assert after["pending_days"] == []
    assert _dash(client)["open_variance_count"] == 0


def test_fixture_pending_on_unsealed_day_blocks_seal_until_repaired(client):
    """draft 批次残留 pending 行：当日虽未封，也不得带病新封。"""
    _plant_legacy(
        batches=[(1, "CB-0001", DAY, 88.0, 50.0, -38.0, 1, "draft", "2026-11-15T23:00:00Z", None)],
        variances=[(1, DAY, -38.0, "pending", None, "2026-11-15T23:00:01Z")],
    )

    r = client.post("/api/close/seal", json={"business_day": DAY, "declared_cash": 50.0, "disposition": "loss"})
    assert r.status_code == 409
    assert "未处置" in r.json()["detail"] or "pending" in r.json()["detail"]

    _repair(client)
    # pending 已按批次快照改判；当日可正常封账（新批次同源写行）
    r = client.post("/api/close/seal", json={"business_day": DAY, "declared_cash": 50.0, "disposition": "loss"})
    assert r.status_code == 200, r.text
    assert _integrity(client)["ok"] is True
    assert _dash(client)["open_variance_count"] == 0


# ---------------------------------------------------------------- 同日多 sealed：重封闸收口

def test_fixture_duplicate_sealed_days_are_demoted(client):
    """同一营业日两个 sealed 批次（重封事故）：只留最新，其余降 draft。"""
    _plant_legacy(
        batches=[
            (1, "CB-0001", DAY, 88.0, 88.0, 0.0, 1, "sealed", "2026-11-15T22:00:00Z", None),
            (2, "CB-0002", DAY, 88.0, 50.0, -38.0, 1, "sealed", "2026-11-15T23:00:00Z", None),
        ],
        variances=[(2, DAY, -38.0, "loss", None, "2026-11-15T23:00:01Z")],
    )
    body = _integrity(client)
    assert "multiple_sealed_batches" in _codes(body)

    _repair(client)
    after = _integrity(client)
    assert after["ok"] is True
    batches = client.get("/api/close/batches").json()
    sealed = [b for b in batches if b["status"] == "sealed" and b["business_day"] == DAY]
    assert len(sealed) == 1 and sealed[0]["batch_no"] == "CB-0002"


# ---------------------------------------------------------------- 长短款表守卫（新库触发器）

def _sealed_day_with_row(client) -> int:
    """走正常流程封一天（认损 -38），返回长短款行 id。"""
    r = client.post("/api/close/seal", json={"business_day": DAY, "declared_cash": 50.0, "disposition": "loss"})
    assert r.status_code == 200, r.text
    return _variances(client)[0]["id"]


def test_table_guard_forbids_update_back_to_pending(client):
    _plant_legacy(batches=[], variances=[])  # 只起守卫，不埋数据
    row_id = _sealed_day_with_row(client)
    from app.db import connect

    conn = connect(immediate=True)
    try:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("UPDATE variance_rows SET disposition='pending' WHERE id=?", (row_id,))
    finally:
        conn.rollback()
        conn.close()


def test_table_guard_forbids_amount_drift_on_update(client):
    _plant_legacy(batches=[], variances=[])
    row_id = _sealed_day_with_row(client)
    from app.db import connect

    conn = connect(immediate=True)
    try:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("UPDATE variance_rows SET variance=-10 WHERE id=?", (row_id,))
    finally:
        conn.rollback()
        conn.close()


def test_table_guard_forbids_delete_on_sealed_batch(client):
    _plant_legacy(batches=[], variances=[])
    row_id = _sealed_day_with_row(client)
    from app.db import connect

    conn = connect(immediate=True)
    try:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("DELETE FROM variance_rows WHERE id=?", (row_id,))
    finally:
        conn.rollback()
        conn.close()


def test_table_guard_forbids_insert_with_foreign_amount(client):
    """新库直接插一行金额≠批差的长短款行：被金额同源触发器拦下。"""
    _plant_legacy(batches=[], variances=[])
    from app.db import connect

    conn = connect(immediate=True)
    try:
        conn.execute(
            """INSERT INTO close_batches(batch_no, business_day, system_total, declared_cash, variance,
                 line_count, status, sealed_at, note)
               VALUES ('X-9', ?, 88, 50, -38, 1, 'draft', 'now', NULL)""",
            (DAY,),
        )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                """INSERT INTO variance_rows(batch_id, business_day, variance, disposition, created_at)
                   VALUES (last_insert_rowid(), ?, -10, 'loss', 'now')""",
                (DAY,),
            )
    finally:
        conn.rollback()
        conn.close()


# ---------------------------------------------------------------- 总览计数同源

def test_dashboard_extended_is_same_source_as_dashboard(client):
    plant_sealed_without_row()
    dash = client.get("/api/dashboard").json()
    ext = client.get("/api/dashboard/extended").json()
    assert ext["open_variance_count"] == dash["open_variance_count"] >= 1
    assert ext["integrity_violation_count"] == dash["integrity_violation_count"] >= 1
    _repair(client)
    ext = client.get("/api/dashboard/extended").json()
    assert ext["open_variance_count"] == 0
