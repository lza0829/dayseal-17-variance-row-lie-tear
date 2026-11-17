"""封账瞬间差额命运 —— 全局互斥策略。

系统只贯彻一种策略（``ACTIVE_VARIANCE_POLICY``）：

先处置再封（``dispose_before_seal``）
    差额非零时，封账请求必须当场选定「认损」(loss, 差额为负) 或
    「认溢」(gain, 差额为正)，并随批次一起写入处置记录；零差额直接以
    ``balanced`` 封账（可附补录校正说明）。未处置的封账请求不会写入封账
    批次，也不会锁日。

另一种策略「先封后挂」（``seal_then_hold``，允许 disposition=pending）
    **不实现**。本模块是差额命运的唯一裁决点，所有封账路径都必须经过
    :func:`resolve_disposition`，从而保证不会出现 pending 状态。
"""
from __future__ import annotations

from typing import Any

from fastapi import HTTPException

# ---------------------------------------------------------------- 策略选择

POLICY_DISPOSE_BEFORE_SEAL = "dispose_before_seal"
POLICY_SEAL_THEN_HOLD = "seal_then_hold"

# 全局唯一生效策略；两种策略互斥，不允许按营业日/收银机混用。
ACTIVE_VARIANCE_POLICY = POLICY_DISPOSE_BEFORE_SEAL

if ACTIVE_VARIANCE_POLICY != POLICY_DISPOSE_BEFORE_SEAL:
    # 防御性检查：本代码库只实现了「先处置再封」。
    raise RuntimeError(f"未实现的差额策略：{ACTIVE_VARIANCE_POLICY}")

POLICY_LABEL = "先处置再封"
POLICY_DESCRIPTION = (
    "差额非零时必须当场选定认损或认溢并写入处置记录后才允许封账；"
    "零差额直接封账；未处置不得生成封账批次、不得锁日。"
)

# ---------------------------------------------------------------- 处置状态

DISPOSITION_BALANCED = "balanced"  # 零差额，直接封账
DISPOSITION_LOSS = "loss"  # 认损 / 短款（实盘小于系统）
DISPOSITION_GAIN = "gain"  # 认溢 / 长款（实盘大于系统）
DISPOSITION_PENDING = "pending"  # 仅属于「先封后挂」策略，本策略禁止

FINAL_DISPOSITIONS = (DISPOSITION_BALANCED, DISPOSITION_LOSS, DISPOSITION_GAIN)
OPEN_DISPOSITIONS = (DISPOSITION_PENDING,)

DISPOSITION_LABELS = {
    DISPOSITION_BALANCED: "零差",
    DISPOSITION_LOSS: "认损",
    DISPOSITION_GAIN: "认溢",
    DISPOSITION_PENDING: "未处置",
}

_ZERO = 1e-9

# 宽松接受前端/调用方的中英文写法，但落库只存规范值。
_ALIASES = {
    "loss": DISPOSITION_LOSS,
    "short": DISPOSITION_LOSS,
    "shortage": DISPOSITION_LOSS,
    "认损": DISPOSITION_LOSS,
    "短款": DISPOSITION_LOSS,
    "亏": DISPOSITION_LOSS,
    "gain": DISPOSITION_GAIN,
    "over": DISPOSITION_GAIN,
    "overage": DISPOSITION_GAIN,
    "surplus": DISPOSITION_GAIN,
    "认溢": DISPOSITION_GAIN,
    "长款": DISPOSITION_GAIN,
    "溢": DISPOSITION_GAIN,
    "balanced": DISPOSITION_BALANCED,
    "balance": DISPOSITION_BALANCED,
    "zero": DISPOSITION_BALANCED,
    "none": DISPOSITION_BALANCED,
    "零差": DISPOSITION_BALANCED,
    "平": DISPOSITION_BALANCED,
    "持平": DISPOSITION_BALANCED,
    "pending": DISPOSITION_PENDING,
    "未处置": DISPOSITION_PENDING,
    "挂账": DISPOSITION_PENDING,
}


def is_final(disposition: str | None) -> bool:
    return disposition in FINAL_DISPOSITIONS


def label(disposition: str | None) -> str:
    return DISPOSITION_LABELS.get(disposition or "", disposition or "")


def describe() -> dict[str, Any]:
    """暴露给前端/接口的策略元数据。"""
    return {
        "variance_policy": ACTIVE_VARIANCE_POLICY,
        "variance_policy_label": POLICY_LABEL,
        "variance_policy_description": POLICY_DESCRIPTION,
        "dispositions": list(FINAL_DISPOSITIONS),
        "disposition_labels": dict(DISPOSITION_LABELS),
        "pending_allowed": False,
    }


def _normalise(requested: Any) -> str | None:
    if requested is None:
        return None
    text = str(requested).strip()
    if not text:
        return None
    return _ALIASES.get(text.lower(), _ALIASES.get(text, text.lower()))


def resolve_disposition(variance: float, requested: Any) -> str:
    """裁决本次封账应落库的 disposition，非法时抛 400。

    - 零差额：强制 ``balanced``（可另附校正说明）。
    - 非零差额：必须显式给出与差额方向一致的 ``loss`` / ``gain``。
    - 任何情况下都不接受 ``pending``，也不接受用备注代替处置状态。
    """
    disposition = _normalise(requested)

    if abs(variance) <= _ZERO:
        if disposition in (None, DISPOSITION_BALANCED):
            return DISPOSITION_BALANCED
        if disposition == DISPOSITION_PENDING:
            raise HTTPException(400, "零差额直接封账为 balanced，无需挂 pending")
        raise HTTPException(
            400,
            "差额为零，只能以 balanced 直接封账（核对补录后差额为零的，可附处置说明）",
        )

    if disposition is None:
        raise HTTPException(
            400,
            f"差额 {variance:.2f} 非零：封账前必须当场选定认损(loss)或认溢(gain)，"
            "未处置不得生成封账批次、不得锁日",
        )
    if disposition == DISPOSITION_PENDING:
        raise HTTPException(
            400,
            "当前策略为「先处置再封」，不接受 disposition=pending；"
            "请先认损(loss)或认溢(gain)",
        )
    if disposition == DISPOSITION_BALANCED:
        raise HTTPException(400, f"差额 {variance:.2f} 非零，不能标记为 balanced")

    expected = DISPOSITION_LOSS if variance < 0 else DISPOSITION_GAIN
    if disposition not in FINAL_DISPOSITIONS:
        raise HTTPException(
            400,
            f"未知处置 {requested!r}；差额为负须认损(loss)，为正须认溢(gain)",
        )
    if disposition != expected:
        direction = "认损(loss)" if expected == DISPOSITION_LOSS else "认溢(gain)"
        raise HTTPException(
            400,
            f"差额 {variance:.2f} 方向不符：本次必须选定 {direction}",
        )
    return expected
