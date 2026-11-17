from __future__ import annotations
import os
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# 隔离数据目录，必须在 import app.* 之前设置。
os.environ["DAYSEAL_DATA"] = tempfile.mkdtemp(prefix="dayseal-test-")

from fastapi.testclient import TestClient  # noqa: E402
from app.main import app  # noqa: E402


@pytest.fixture()
def client():
    """每个用例一份全新数据库（含 seed 流水）。"""
    from app.config import DB_PATH
    from app.seed import seed_if_empty

    if DB_PATH.exists():
        DB_PATH.unlink()
    seed_if_empty()
    with TestClient(app) as c:
        yield c


DAY = "2026-11-15"  # seed 中只有一条 88.00 的流水，便于控制差额
SYSTEM_TOTAL = 88.0


def seal(client, *, declared, disposition=None, day=DAY, note=None, correction_note=None,
         correction_lines=None):
    body = {"business_day": day, "declared_cash": declared}
    if disposition is not None:
        body["disposition"] = disposition
    if note is not None:
        body["note"] = note
    if correction_note is not None:
        body["correction_note"] = correction_note
    if correction_lines is not None:
        body["correction_lines"] = correction_lines
    return client.post("/api/close/seal", json=body)


def state(client, day=DAY):
    return client.get(f"/api/close/preview?business_day={day}").json()


def all_batches(client):
    return client.get("/api/close/batches").json()


def all_variances(client):
    return client.get("/api/close/variances").json()


def assert_not_sealed_without_trace(client):
    assert state(client)["sealed"] is False
    assert state(client)["locked"] is False
    assert all_batches(client) == []
    assert all_variances(client) == []


# ---------------------------------------------------------------- 策略唯一性

def test_policy_is_single_global_strategy(client):
    p = client.get("/api/close/policy").json()
    assert p["variance_policy"] == "dispose_before_seal"
    assert p["pending_allowed"] is False
    assert "pending" not in p["dispositions"]


def test_pending_is_not_an_offered_disposition(client):
    policy = client.get("/api/close/policy").json()
    assert policy["variance_policy"] == "dispose_before_seal"
    assert policy["pending_allowed"] is False
    dashboard_policy = client.get("/api/dashboard").json()["variance_policy"]
    assert dashboard_policy["variance_policy"] == "dispose_before_seal"
    assert dashboard_policy["pending_allowed"] is False


# ---------------------------------------------------------------- 零差额直接封

def test_zero_variance_seals_directly(client):
    r = seal(client, declared=SYSTEM_TOTAL)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["variance"] == 0.0
    assert body["status"] == "sealed"
    assert body["locked"] is True
    assert body["disposition"] == "balanced"

    s = state(client)
    assert s["sealed"] is True and s["locked"] is True
    assert s["disposition"] == "balanced"
    assert s["variance_row"]["variance"] == 0.0

    rows = all_variances(client)
    assert len(rows) == 1 and rows[0]["disposition"] == "balanced"


def test_zero_variance_with_correction_note_is_still_balanced(client):
    r = seal(client, declared=SYSTEM_TOTAL, correction_note="补录一笔抹零校正，差额归零")
    assert r.status_code == 200, r.text
    row = all_variances(client)[0]
    assert row["disposition"] == "balanced"
    assert "补录" in row["note"]


def test_zero_variance_cannot_be_marked_as_loss(client):
    r = seal(client, declared=SYSTEM_TOTAL, disposition="loss")
    assert r.status_code == 400
    assert_not_sealed_without_trace(client)


# ---------------------------------------------------------------- 有差额：必须先处置

@pytest.mark.parametrize("declared,expected", [(50.0, "loss"), (120.0, "gain")])
def test_nonzero_without_disposition_blocks_seal(client, declared, expected):
    r = seal(client, declared=declared)
    assert r.status_code == 400
    assert expected in r.json()["detail"] or "认损" in r.json()["detail"] or "认溢" in r.json()["detail"]
    # 未处置：不得写批次、不得锁日、不得写处置行
    assert_not_sealed_without_trace(client)


def test_note_text_cannot_impersonate_disposition(client):
    r = seal(client, declared=50.0, note="已认损，责任人张三")
    assert r.status_code == 400
    assert_not_sealed_without_trace(client)


def test_pending_disposition_rejected(client):
    r = seal(client, declared=50.0, disposition="pending")
    assert r.status_code == 400
    assert "pending" in r.json()["detail"]
    assert_not_sealed_without_trace(client)


@pytest.mark.parametrize("declared,wrong,right", [
    (-20.0, "gain", "loss"),   # 68.0 短款
    (20.0, "loss", "gain"),    # 108.0 长款
])
def test_direction_must_match_sign(client, declared, wrong, right):
    r = seal(client, declared=SYSTEM_TOTAL + declared, disposition=wrong)
    assert r.status_code == 400
    assert "方向不符" in r.json()["detail"]
    assert_not_sealed_without_trace(client)


def test_nonzero_balanced_disposition_rejected(client):
    r = seal(client, declared=50.0, disposition="balanced")
    assert r.status_code == 400
    assert_not_sealed_without_trace(client)


def test_nonzero_unknown_disposition_rejected(client):
    r = seal(client, declared=50.0, disposition="write-off")
    assert r.status_code == 400
    assert_not_sealed_without_trace(client)


# ---------------------------------------------------------------- 有差额边界：认损

def test_loss_seal_writes_batch_row_and_lock_together(client):
    r = seal(client, declared=50.0, disposition="loss", correction_note="收银员自赔")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["variance"] == -38.0
    assert body["disposition"] == "loss"
    assert body["status"] == "sealed" and body["locked"] is True

    b = all_batches(client)
    assert len(b) == 1
    assert b[0]["status"] == "sealed"
    assert b[0]["disposition"] == "loss"
    assert b[0]["variance"] == -38.0

    rows = all_variances(client)
    assert len(rows) == 1
    assert rows[0]["batch_id"] == b[0]["id"]
    assert rows[0]["disposition"] == "loss"
    assert rows[0]["note"] == "收银员自赔"

    s = state(client)
    assert s["sealed"] and s["locked"] and s["disposition"] == "loss"


# ---------------------------------------------------------------- 有差额边界：认溢

def test_gain_seal_writes_batch_row_and_lock_together(client):
    r = seal(client, declared=100.0, disposition="认溢")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["variance"] == 12.0
    assert body["disposition"] == "gain"
    assert body["locked"] is True

    b = all_batches(client)
    assert b[0]["status"] == "sealed" and b[0]["disposition"] == "gain"
    rows = all_variances(client)
    assert rows[0]["disposition"] == "gain"
    assert rows[0]["variance"] == 12.0


# ---------------------------------------------------------------- 零差补录校正

def test_correction_line_brings_variance_to_zero_then_balances(client):
    # 系统 88，实盘 90（长款 2）→ 补录一笔 +2 的真实流水后归零
    r = seal(
        client,
        declared=90.0,
        correction_lines=[{"amount": 2.0, "note": "补记漏登的一笔现金单"}],
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["variance"] == 0.0
    assert body["disposition"] == "balanced"
    assert body["locked"] is True

    s = state(client)
    assert s["system_total"] == 90.0
    assert s["line_count"] == 2
    assert s["sealed"] is True
    assert s["disposition"] == "balanced"

    # 补录必须是真实流水，而不是备注文案
    notes = [l["note"] for l in client.get(f"/api/lines?business_day={DAY}").json()]
    assert "补记漏登的一笔现金单" in notes

    row = all_variances(client)[0]
    assert row["disposition"] == "balanced"
    assert "补录" in row["note"]


def test_correction_wrong_amount_still_rejected(client):
    r = seal(client, declared=90.0, correction_lines=[{"amount": 1.0}])
    assert r.status_code == 400
    assert "不符" in r.json()["detail"]
    assert_not_sealed_without_trace(client)
    # 被拒后流水不得残留
    assert state(client)["system_total"] == SYSTEM_TOTAL
    assert state(client)["line_count"] == 1


def test_correction_cannot_be_combined_with_loss(client):
    r = seal(
        client,
        declared=90.0,
        disposition="loss",
        correction_lines=[{"amount": 2.0}],
    )
    assert r.status_code == 400
    assert "互斥" in r.json()["detail"]
    assert_not_sealed_without_trace(client)


def test_zero_amount_correction_rejected(client):
    r = seal(client, declared=90.0, correction_lines=[{"amount": 0}])
    assert r.status_code == 400
    assert_not_sealed_without_trace(client)


def test_note_only_is_not_a_correction(client):
    r = seal(client, declared=90.0, correction_note="已补录一笔 +2 的现金单")
    assert r.status_code == 400
    assert_not_sealed_without_trace(client)


# ---------------------------------------------------------------- 封后写流水仍拒

@pytest.mark.parametrize("seal_kwargs", [
    dict(declared=SYSTEM_TOTAL),                       # 零差
    dict(declared=50.0, disposition="loss"),           # 认损
    dict(declared=100.0, disposition="gain"),          # 认溢
])
def test_lines_are_frozen_after_seal(client, seal_kwargs):
    assert seal(client, **seal_kwargs).status_code == 200
    day = DAY

    created = client.post("/api/lines", json={
        "business_day": day, "till_code": "TILL-A", "amount": 10,
    })
    assert created.status_code == 409
    assert "已封账" in created.json()["detail"]

    # seed 中 DAY 的流水 id
    line_id = client.get(f"/api/lines?business_day={day}").json()[0]["id"]
    patched = client.patch(f"/api/lines/{line_id}", json={"amount": 99})
    assert patched.status_code == 409
    removed = client.delete(f"/api/lines/{line_id}")
    assert removed.status_code == 409

    # 流水与合计未被动过
    assert state(client)["system_total"] == SYSTEM_TOTAL


def test_other_day_remains_open_after_seal(client):
    assert seal(client, declared=50.0, disposition="loss").status_code == 200
    # 本策略只约束封账瞬间；另一营业日不受影响，但仍须先处置
    other = "2026-11-16"
    other_total = sum(
        l["amount"] for l in client.get(f"/api/lines?business_day={other}").json()
    )
    blocked = seal(client, declared=other_total + 10, day=other)
    assert blocked.status_code == 400
    assert state(client, other)["sealed"] is False
    ok = seal(client, declared=other_total + 10, disposition="gain", day=other)
    assert ok.status_code == 200, ok.text
    assert state(client, DAY)["sealed"] is True


# ---------------------------------------------------------------- 重封 / 状态互斥

def test_reseal_rejected(client):
    assert seal(client, declared=SYSTEM_TOTAL).status_code == 200
    again = seal(client, declared=SYSTEM_TOTAL)
    assert again.status_code == 409
    assert len(all_batches(client)) == 1


def test_failed_seal_leaves_day_fully_open(client):
    assert seal(client, declared=50.0).status_code == 400
    assert_not_sealed_without_trace(client)
    # 处置失败后仍可正常补齐并封账
    assert seal(client, declared=50.0, disposition="loss").status_code == 200


# ---------------------------------------------------------------- 同源约束（DB 层）

def test_db_forbids_sealed_batch_without_variance_row(client):
    import sqlite3
    from app.db import connect

    conn = connect(immediate=True)
    try:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                '''INSERT INTO close_batches(
                     batch_no, business_day, system_total, declared_cash, variance,
                     line_count, status, sealed_at, note
                   ) VALUES ('X-1','2026-11-15',88,88,0,1,'sealed','now',NULL)'''
            )
    finally:
        conn.rollback()
        conn.close()


def test_db_forbids_pending_disposition_row(client):
    import sqlite3
    from app.db import connect

    conn = connect(immediate=True)
    try:
        conn.execute(
            '''INSERT INTO close_batches(
                 batch_no, business_day, system_total, declared_cash, variance,
                 line_count, status, sealed_at, note
               ) VALUES ('X-2','2026-11-15',88,50,-38,1,'draft','now',NULL)'''
        )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                '''INSERT INTO variance_rows(batch_id,business_day,variance,disposition,created_at)
                   VALUES (last_insert_rowid(),'2026-11-15',-38,'pending','now')'''
            )
    finally:
        conn.rollback()
        conn.close()


def test_db_forbids_direction_mismatch_row(client):
    import sqlite3
    from app.db import connect

    conn = connect(immediate=True)
    try:
        conn.execute(
            '''INSERT INTO close_batches(
                 batch_no, business_day, system_total, declared_cash, variance,
                 line_count, status, sealed_at, note
               ) VALUES ('X-3','2026-11-15',88,50,-38,1,'draft','now',NULL)'''
        )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                '''INSERT INTO variance_rows(batch_id,business_day,variance,disposition,created_at)
                   VALUES (last_insert_rowid(),'2026-11-15',-38,'gain','now')'''
            )
    finally:
        conn.rollback()
        conn.close()


def test_integrity_endpoint_reports_clean_state(client):
    assert seal(client, declared=50.0, disposition="loss").status_code == 200
    body = client.get("/api/close/integrity").json()
    assert body["ok"] is True
    assert body["violations"] == []


def test_dashboard_exposes_policy_and_open_count(client):
    dash = client.get("/api/dashboard").json()
    assert dash["variance_policy"]["variance_policy"] == "dispose_before_seal"
    assert dash["open_variance_count"] == 0

    assert seal(client, declared=100.0, disposition="gain", day="2026-11-16").status_code == 400
    other_total = sum(
        l["amount"] for l in client.get("/api/lines?business_day=2026-11-16").json()
    )
    r = seal(client, declared=other_total + 5, disposition="gain", day="2026-11-16")
    assert r.status_code == 200, r.text
    dash = client.get("/api/dashboard").json()
    assert dash["open_variance_count"] == 0  # 已认溢不属于未处置
    assert dash["integrity_violation_count"] == 0
