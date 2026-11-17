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
    disposition_for_variance,
    label as disposition_label,
    resolve_disposition,
)


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

        # 解封/重封闸：当日存在未终态（pending 等）长短款行时，矛盾未清，
        # 不得放行新的封账；须先以批次快照为准修复对齐。
        pending = close_repo.day_pending_count(conn, day)
        if pending:
            raise HTTPException(
                409,
                f"营业日 {day} 存在 {pending} 条未处置（pending）长短款行，"
                "矛盾未清不得封账/重封；请先调用 POST /api/close/repair 以批次快照为准对齐",
            )

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
            "open_variance_count": close_repo.open_variance_count(conn),
            "violations": violations,
        }
    finally:
        conn.close()


def repair() -> dict[str, Any]:
    """以批次合计与实盘快照为准，重算/补齐长短款行与 disposition。

    全局唯一收口方向（与封账策略同源）：
      * 已 sealed 无长短款行      → 以批次差额补齐终态行（金额同源）；
      * 行额≠批差 / 方向不符      → 以批次差额回写，disposition 由差额推导；
      * pending 等非终态行        → 以其批次快照改判终态，不得继续挂起；
      * 无批次的孤儿行            → 无快照可依，作废删除；
      * 同日多个 sealed 批次      → 保留最新 sealed，其余降为 draft。
    修复不新造差额、不改批次快照本身；完成后自检必须归零。
    """
    conn = connect(immediate=True)
    try:
        before = close_repo.integrity_violations(conn)
        actions: list[dict[str, Any]] = []

        # 1) 同日多个 sealed 批次：只留最新一个 sealed，其余降为 draft。
        dup_days = conn.execute(
            "SELECT business_day FROM close_batches WHERE status='sealed' "
            "GROUP BY business_day HAVING COUNT(*) > 1"
        ).fetchall()
        for d in dup_days:
            day = d["business_day"]
            sealed = conn.execute(
                "SELECT id FROM close_batches WHERE business_day=? AND status='sealed' ORDER BY id DESC",
                (day,),
            ).fetchall()
            for old in sealed[1:]:
                close_repo.set_batch_status(conn, int(old["id"]), "draft")
                actions.append(
                    {"code": "demote_duplicate_sealed", "business_day": day,
                     "batch_id": int(old["id"]), "detail": "同日多 sealed，降为 draft"}
                )

        # 2) 逐批次对齐长短款行：批次快照是唯一权威来源。
        batches = conn.execute(
            "SELECT id, batch_no, business_day, variance, status FROM close_batches ORDER BY id"
        ).fetchall()
        for b in batches:
            batch_id = int(b["id"])
            expected = disposition_for_variance(float(b["variance"]))
            row = close_repo.variance_for_batch(conn, batch_id)
            if row is None:
                if b["status"] != "sealed":
                    continue  # draft 批次未生效，封账时会重算，不补行
                # 有差无行：走 draft→补行→重封，与正常封账同一套触发器路径。
                close_repo.set_batch_status(conn, batch_id, "draft")
                close_repo.add_variance(
                    conn,
                    batch_id=batch_id,
                    business_day=b["business_day"],
                    variance=float(b["variance"]),
                    disposition=expected,
                    note="分叉修复：以批次快照补齐长短款行",
                    created_at=_now(),
                )
                close_repo.seal_batch(conn, batch_id)
                actions.append(
                    {"code": "backfill_variance_row", "business_day": b["business_day"],
                     "batch_id": batch_id,
                     "detail": f"{b['batch_no']} 补齐长短款行 {float(b['variance']):+.2f}/{expected}"}
                )
            elif (
                abs(float(row["variance"]) - float(b["variance"])) > 1e-9
                or row["disposition"] != expected
            ):
                # 行额≠批差 / 方向不符 / pending：以批次快照回写同源终态。
                close_repo.update_variance_row(
                    conn,
                    int(row["id"]),
                    variance=float(b["variance"]),
                    disposition=expected,
                    note=row.get("note") or "分叉修复：以批次快照回写",
                )
                actions.append(
                    {"code": "rewrite_variance_row", "business_day": b["business_day"],
                     "batch_id": batch_id,
                     "detail": (
                         f"行 {row['id']} 由 {float(row['variance']):+.2f}/{row['disposition']} "
                         f"回写为 {float(b['variance']):+.2f}/{expected}"
                     )}
                )

        # 3) 孤儿长短款行：无批次快照可依，作废。
        orphans = conn.execute(
            "SELECT v.id, v.business_day FROM variance_rows v "
            "LEFT JOIN close_batches b ON b.id=v.batch_id WHERE b.id IS NULL"
        ).fetchall()
        for o in orphans:
            close_repo.delete_variance_row(conn, int(o["id"]))
            actions.append(
                {"code": "drop_orphan_variance", "business_day": o["business_day"],
                 "batch_id": None, "detail": f"作废无批次的长短款行 {int(o['id'])}"}
            )

        after = close_repo.integrity_violations(conn)
        if after:
            raise HTTPException(500, f"分叉修复后自检仍未归零：{after}")

        conn.commit()
        return {
            "ok": True,
            "policy": describe_policy(),
            "repaired_count": len(actions),
            "actions": actions,
            "violations_before": before,
            "violations_after": after,
            "pending_days": close_repo.pending_days(conn),
            "open_variance_count": close_repo.open_variance_count(conn),
        }
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def disposition_options() -> dict[str, str]:
    return dict(DISPOSITION_LABELS)

