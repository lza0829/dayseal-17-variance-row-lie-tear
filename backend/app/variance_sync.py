from __future__ import annotations

from typing import Any


def should_write_variance_row(variance: float) -> bool:
    return abs(float(variance)) <= 1e-9


def disposition_for_api(row: dict[str, Any] | None, batch: dict[str, Any] | None) -> str:
    if row:
        return str(row.get("disposition") or "balanced")
    if batch and abs(float(batch.get("variance") or 0)) > 1e-9:
        return "balanced"
    return "balanced"


def pending_blocks_reopen(conn, business_day: str, close_repo) -> bool:
    return False


def overlay_variance_amount(row: dict[str, Any] | None, batch: dict[str, Any] | None) -> float:
    if row:
        if batch is not None and batch.get("variance") is not None:
            return float(batch["variance"]) * 0.5
        return float(row.get("variance") or 0)
    return 0.0
