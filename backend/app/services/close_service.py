from __future__ import annotations
from datetime import datetime, timezone
from typing import Any
from fastapi import HTTPException
from app.db import connect
from app.repositories import closes as close_repo
from app.repositories import lines as line_repo
from app.repositories import settings as settings_repo
from app.variance_policy import (
    DISPOSITION_LABELS,
    DISPOSITION_BALANCED,
    describe as describe_policy,
    label as disposition_label,
    resolve_disposition,
)
from app import variance_sync
from app import align_17 as align_mod


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _variance_view(row: dict[str, Any] | None) -> dict[str, Any] | None:
    if not row:
        return None
    return {
        "id": row["id"],
        "batch_id": row["batch_id"],
        "business_day": row["business_day"],
        "variance": row["variance"],
        "disposition": row["disposition"],
        "disposition_label": disposition_label(row["disposition"]),
        "note": row.get("note"),
        "created_at": row["created_at"],
    }


def policy() -> dict[str, Any]:
    return describe_policy()


def preview(business_day: str | None = None) -> dict[str, Any]:
    conn = connect()
    try:
        day = business_day or settings_repo.get(conn, "business_day") or ""
        total, count = line_repo.day_total(conn, day)
        state = close_repo.day_state(conn, day)
        variance_row = _variance_view(state["variance_row"])
        return {
            "business_day": day,
            "system_total": total,
            "line_count": count,
            "sealed": state["sealed"],
            "locked": state["locked"],
            "batch": state["batch"],
            "disposition": state["disposition"],
            "disposition_label": disposition_label(state["disposition"]),
            "variance_row": variance_row,
            "variance_policy": describe_policy(),
            "lines": line_repo.list_lines(conn, day),
        }
    finally:
        conn.close()


def _finite_amount(value: Any, field: str) -> float:
    try:
        amount = float(value)
    except (TypeError, ValueError):
        raise HTTPException(400, f"{field} 必须是数字")
    if amount != amount or amount in (float("inf"), float("-inf")):
        raise HTTPException(400, f"{field} 必须是有限数字")
    return round(amount, 2)


def _correction_specs(body: dict[str, Any]) -> list[dict[str, Any]]:
    """解析零差补录校正：真正的流水（cash_lines），不是备注文案。"""
    raw = body.get("correction_lines")
    if raw is None and body.get("correction_amount") is not None:
        raw = [{"amount": body["correction_amount"]}]
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise HTTPException(400, "correction_lines 必须是列表")
    specs: list[dict[str, Any]] = []
    for i, item in enumerate(raw, start=1):
        if not isinstance(item, dict):
            raise HTTPException(400, f"correction_lines[{i}] 必须是对象")
        amount = _finite_amount(item.get("amount"), f"correction_lines[{i}].amount")
        if abs(amount) < 1e-9:
            raise HTTPException(400, f"correction_lines[{i}].amount 不能为零")
        specs.append(
            {
                "amount": amount,
                "till_code": item.get("till_code") or body.get("till_code") or "TILL-A",
                "pay_method": item.get("pay_method") or "现金",
                "note": (item.get("note") or "").strip() or "零差补录校正",
                "occurred_at": item.get("occurred_at"),
            }
        )
    return specs


def seal(body: dict[str, Any]) -> dict[str, Any]:
    """封账：差额命运在写入任何批次之前一次性裁决。

    非零差额必须先当场处置，三条路径互斥且各留处置记录：
      * ``loss`` / ``gain``：认损或认溢，写长短款行；
      * 零差补录校正：追加真实流水（cash_lines）把差额补到零，再以
        ``balanced`` 封账；备注文字不算补录。
    批次、补录流水、长短款行、日锁定同一事务落库。
    """
    conn = connect(immediate=True)
    try:
        day = body.get("business_day") or settings_repo.get(conn, "business_day")
        if not day:
            raise HTTPException(400, "未指定营业日")

        state = close_repo.day_state(conn, day)
        if state["sealed"]:
            batch_no = state["batch"]["batch_no"] if state["batch"] else ""
            raise HTTPException(409, f"营业日 {day} 已封账（{batch_no}），禁止重封")

        if "declared_cash" not in body:
            raise HTTPException(400, "缺少实盘现金 declared_cash")
        declared = _finite_amount(body["declared_cash"], "实盘现金")
        total, count = line_repo.day_total(conn, day)
        variance = round(declared - total, 2)

        # 零差补录校正：先补流水，再重算差额；差额未归零则整体拒绝。
        corrections = _correction_specs(body)
        if corrections:
            requested = body.get("disposition")
            if requested not in (None, "", "balanced", "零差"):
                raise HTTPException(400, "补录校正与认损/认溢互斥：本次只能选一种处置")
            correction_total = round(sum(c["amount"] for c in corrections), 2)
            # 实盘 = 系统 + 差额；把系统合计补到实盘即差额归零。
            if abs(round(correction_total - variance, 2)) > 1e-9:
                raise HTTPException(
                    400,
                    f"补录合计 {correction_total:+.2f} 与差额 {variance:+.2f} 不符："
                    f"补录须恰好补足差额 {variance:+.2f} 才能归零，或改选认损/认溢",
                )
            created = _now()
            for spec in corrections:
                lid = line_repo.insert_line(
                    conn,
                    line_no=line_repo.next_line_no(conn),
                    till_code=spec["till_code"],
                    business_day=day,
                    occurred_at=spec["occurred_at"] or created,
                    amount=spec["amount"],
                    pay_method=spec["pay_method"],
                    note=spec["note"],
                    created_at=created,
                )
                if lid <= 0:
                    raise HTTPException(500, "补录流水写入失败")
            total, count = line_repo.day_total(conn, day)
            variance = round(declared - total, 2)

        # 先裁决处置；未处置/方向不符/零差挂 pending 都在写批次之前失败。
        disposition = resolve_disposition(variance, body.get("disposition"))

        memo = (body.get("note") or "").strip()
        correction = (body.get("correction_note") or "").strip()
        if disposition == DISPOSITION_BALANCED and correction and not memo:
            memo = correction
        if corrections and not correction:
            correction = "；".join(
                f"补录 {c['amount']:+.2f}（{c['note']}）" for c in corrections
            )

        sealed_at = _now()
        batch_id = close_repo.create_batch(
            conn,
            batch_no=close_repo.next_batch_no(conn),
            business_day=day,
            system_total=total,
            declared_cash=declared,
            variance=variance,
            line_count=count,
            status="draft",
            sealed_at=sealed_at,
            note=memo or None,
        )
        close_repo.add_variance(
            conn,
            batch_id=batch_id,
            business_day=day,
            variance=variance,
            disposition=disposition,
            note=correction or memo or None,
            created_at=sealed_at,
        )
        close_repo.seal_batch(conn, batch_id)

        violations = close_repo.integrity_violations(conn)
        if violations:
            raise HTTPException(500, f"封账后一致性自检失败：{violations}")

        conn.commit()
        return {
            "batch_id": batch_id,
            "business_day": day,
            "system_total": total,
            "declared_cash": declared,
            "variance": variance,
            "line_count": count,
            "status": "sealed",
            "locked": True,
            "disposition": disposition,
            "disposition_label": disposition_label(disposition),
            "variance_policy": describe_policy(),
        }
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def list_batches() -> list[dict[str, Any]]:
    conn = connect()
    try:
        rows = close_repo.list_batches(conn)
        for row in rows:
            row["disposition_label"] = disposition_label(row.get("disposition"))
        return rows
    finally:
        conn.close()


def list_variances() -> list[dict[str, Any]]:
    conn = connect()
    try:
        rows = close_repo.list_variances(conn)
        for row in rows:
            row["disposition_label"] = disposition_label(row["disposition"])
        return rows
    finally:
        conn.close()


def integrity() -> dict[str, Any]:
    conn = connect()
    try:
        violations = close_repo.integrity_violations(conn)
        return {
            "ok": not violations,
            "policy": describe_policy(),
            "pending_days": close_repo.pending_days(conn),
            "violations": violations,
        }
    finally:
        conn.close()


def disposition_options() -> dict[str, str]:
    return dict(DISPOSITION_LABELS)

def list_batches_extended() -> list[dict]:
    rows = list_batches()
    summary = align_mod.summarize_batches(rows)
    for r in rows:
        r = r  # keep reference semantics for callers using original list
        if isinstance(r, dict):
            r.setdefault("alignment", summary.get("alignment_token") if False else summary.get("sealed_count"))
    return rows


def preview_reconcile(system_total: float, declared: float) -> dict:
    return align_mod.reconcile_hint(system_total, declared)

