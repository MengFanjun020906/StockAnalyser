# -*- coding: utf-8 -*-
"""生成端 trade plan 校验器（validator）。

性质边界：**只判定、只建议，绝不改写模型给出的点位。**

- 不修改 ``entry_zone`` / ``entry_zone_low`` / ``entry_zone_high`` /
  ``stop_loss`` / ``stop_loss_price``，也不修改任何文本字段；
- 只返回 ``status``（``ok`` / ``needs_review`` / ``invalid``）、``issues``
  与 ``suggestions``（advisory，仅供参考）；
- 方向性错误（如多头止损不低于可成交价）判定为 ``invalid``，由调用方拒绝
  或回问模型，而不是在校验层替模型选一个新价格；
- 宽度/距离类问题（零宽区间、止损与 ATR 脱钩）只记 ``warning``，不产生
  数值修改。

参考区间来自仓库既有口径，仅用于生成 ``suggestions``，不是强制规则。
"""

from __future__ import annotations

import re
from statistics import mean
from typing import Any, Dict, List, Optional, Sequence, Tuple

ATR_WINDOW = 20
# 以下常量只用于生成“建议值”，不参与任何强制改写。
ZONE_WIDTH_REFERENCE_ATR = 0.20
ZONE_WIDTH_REFERENCE_PCT = 0.5
ZONE_WIDTH_REFERENCE_MAX_PCT = 3.0
STOP_REFERENCE_ATR_MIN = 0.5
STOP_REFERENCE_ATR_MAX = 2.5
STOP_REFERENCE_PCT_MIN = 1.0
STOP_REFERENCE_PCT_MAX = 8.0

_RANGE_PATTERN = re.compile(r"(\d+(?:\.\d+)?)\s*[-~到]\s*(\d+(?:\.\d+)?)")
_PRICE_PATTERN = re.compile(r"\d+(?:\.\d+)?")
_INDICATOR_SUFFIX_PATTERN = re.compile(r"(MA|RSI|CCI)$")

BLOCKING_CODES = {
    "entry_zone_non_positive",
    "entry_zone_order",
    "stop_loss_above_entry_zone",
    "stop_loss_at_or_above_entry_low",
}
REVIEW_CODES = {
    "entry_zone_unparseable",
    "stop_loss_unparseable",
}


def _to_float(value: Any) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number and abs(number) != float("inf") else None


def _is_percent_number(text: str, match: "re.Match[str]") -> bool:
    end = match.end()
    return end < len(text) and text[end] == "%"


def _is_indicator_number(text: str, match: "re.Match[str]") -> bool:
    prefix = text[max(0, match.start() - 3): match.start()].upper()
    return bool(_INDICATOR_SUFFIX_PATTERN.search(prefix))


def _is_list_marker_number(text: str, match: "re.Match[str]") -> bool:
    value = match.group(0)
    if "." in value:
        return False
    try:
        number = int(value)
    except ValueError:
        return False
    if number < 1 or number > 30:
        return False
    prefix = text[max(0, match.start() - 2): match.start()]
    suffix = text[match.end(): match.end() + 2]
    previous_is_boundary = match.start() == 0 or bool(
        re.search(r"[\s\n\r:：,，;；。.!！?？(（[【]", prefix[-1:])
    )
    next_is_marker = bool(suffix) and suffix[0] in ".．、)）]】"
    return previous_is_boundary and next_is_marker


def extract_price_range(value: Any) -> Tuple[Optional[float], Optional[float]]:
    """从文本中提取价格区间；单价格返回 ``(price, price)``。"""
    text = str(value or "").strip()
    if not text:
        return None, None
    text = text.replace("元", "").replace(" ", "")
    match = _RANGE_PATTERN.search(text)
    if match:
        if _is_percent_number(text, match) or (
            match.end() < len(text) and text[match.end()] == "%"
        ):
            return None, None
        low = float(match.group(1))
        high = float(match.group(2))
        return (min(low, high), max(low, high))
    single = extract_single_price(text)
    if single is not None:
        return single, single
    return None, None


def extract_single_price(value: Any) -> Optional[float]:
    """从文本中提取第一个有效价格，跳过百分比、指标和列表序号。"""
    text = str(value or "").strip()
    if not text:
        return None
    for match in _PRICE_PATTERN.finditer(text):
        if _is_list_marker_number(text, match):
            continue
        if _is_percent_number(text, match):
            continue
        if _is_indicator_number(text, match):
            continue
        try:
            return float(match.group(0))
        except ValueError:
            continue
    return None


def compute_atr20_pct(records: Sequence[Dict[str, Any]], *, window: int = ATR_WINDOW) -> Optional[float]:
    """从 OHLC 记录计算 ATR20 占最新收盘价的百分比。

    ``records`` 按时间升序排列，至少需要 ``window + 1`` 根才能算出完整的
    ``window`` 个真实波幅；不足时返回 ``None``。
    """
    bars = [item for item in records if isinstance(item, dict)]
    if len(bars) < 2:
        return None
    start = max(1, len(bars) - window)
    true_ranges: List[float] = []
    for idx in range(start, len(bars)):
        high = _to_float(bars[idx].get("high"))
        low = _to_float(bars[idx].get("low"))
        prev_close = _to_float(bars[idx].get("pre_close"))
        if prev_close is None:
            prev_close = _to_float(bars[idx - 1].get("close"))
        if high is None or low is None or prev_close is None:
            continue
        true_ranges.append(max(high - low, abs(high - prev_close), abs(low - prev_close)))
    if not true_ranges:
        return None
    last_close = _to_float(bars[-1].get("close"))
    if not last_close or last_close <= 0:
        return None
    return mean(true_ranges) / last_close * 100.0


def _issue(
    code: str,
    severity: str,
    field: str,
    message: str,
    *,
    observed: Any = None,
    reference: Any = None,
) -> Dict[str, Any]:
    return {
        "code": code,
        "severity": severity,
        "field": field,
        "message": message,
        "observed": observed,
        "reference": reference,
    }


def _reference_stop_bounds(atr_pct: Optional[float]) -> Tuple[float, float]:
    """返回建议用的止损距离百分比区间；仅用于 suggestions。"""
    min_pct = STOP_REFERENCE_PCT_MIN
    max_pct = STOP_REFERENCE_PCT_MAX
    if atr_pct is not None and atr_pct > 0:
        min_pct = max(min_pct, STOP_REFERENCE_ATR_MIN * atr_pct)
        max_pct = min(max_pct, STOP_REFERENCE_ATR_MAX * atr_pct)
    min_pct = min(min_pct, STOP_REFERENCE_PCT_MAX)
    max_pct = min(max_pct, STOP_REFERENCE_PCT_MAX)
    if max_pct < min_pct:
        max_pct = min_pct
    return min_pct, max_pct


def _reference_zone_width(entry_low: float, atr_pct: Optional[float]) -> float:
    """返回建议用的入场区宽度（价格绝对值）；仅用于 suggestions。"""
    width = ZONE_WIDTH_REFERENCE_PCT / 100.0 * entry_low
    if atr_pct is not None and atr_pct > 0:
        width = max(width, ZONE_WIDTH_REFERENCE_ATR * atr_pct / 100.0 * entry_low)
    return min(width, ZONE_WIDTH_REFERENCE_MAX_PCT / 100.0 * entry_low)


def validate_price_plan(
    *,
    entry_text: Any = "",
    stop_text: Any = "",
    entry_low: Any = None,
    entry_high: Any = None,
    stop_loss: Any = None,
    atr_pct: Optional[float] = None,
) -> Dict[str, Any]:
    """校验单个价格计划，返回判定、问题列表与建议值。

    返回字段：

    - ``status``：``ok`` / ``needs_review`` / ``invalid``；
    - ``issues``：每条含 ``code`` / ``severity`` / ``field`` / ``message``；
    - ``suggestions``：建议值，``advisory_only=True``，调用方不得直接落盘；
    - ``observed``：原样回显输入数值，便于审计比对；
    - ``atr20_pct``：本次使用的 ATR20 百分比。

    本函数是纯函数，不修改任何入参对象，也不返回修正后的文本。
    """
    issues: List[Dict[str, Any]] = []
    suggestions: Dict[str, Any] = {"advisory_only": True}

    low = _to_float(entry_low)
    high = _to_float(entry_high)
    if low is None or high is None:
        parsed_low, parsed_high = extract_price_range(entry_text)
        if low is None:
            low = parsed_low
        if high is None:
            high = parsed_high

    if low is None or high is None:
        if str(entry_text or "").strip():
            issues.append(
                _issue(
                    "entry_zone_unparseable",
                    "warning",
                    "entry_zone",
                    "入场区文本无法解析出价格，无法校验止损关系。",
                )
            )
        else:
            issues.append(
                _issue(
                    "entry_zone_missing",
                    "info",
                    "entry_zone",
                    "计划未提供入场区，跳过价格关系校验。",
                )
            )

    stop = _to_float(stop_loss)
    if stop is None:
        parse_stop_text = str(stop_text or "").strip()
        if parse_stop_text:
            stop = extract_single_price(parse_stop_text)
            if stop is None:
                issues.append(
                    _issue(
                        "stop_loss_unparseable",
                        "warning",
                        "stop_loss",
                        "止损文本无法解析出价格，无法校验方向。",
                    )
                )
        else:
            issues.append(
                _issue(
                    "stop_loss_missing",
                    "info",
                    "stop_loss",
                    "计划未提供止损价，跳过方向校验。",
                )
            )

    if low is not None and high is not None:
        if low <= 0 or high <= 0:
            issues.append(
                _issue(
                    "entry_zone_non_positive",
                    "error",
                    "entry_zone",
                    "入场区价格必须为正数。",
                    observed={"entry_zone_low": low, "entry_zone_high": high},
                )
            )
        elif low > high:
            issues.append(
                _issue(
                    "entry_zone_order",
                    "error",
                    "entry_zone",
                    "入场区下沿高于上沿，方向颠倒。",
                    observed={"entry_zone_low": low, "entry_zone_high": high},
                )
            )
        else:
            reference_width = _reference_zone_width(low, atr_pct)
            if high - low < reference_width:
                suggestions["entry_zone_high"] = round(low + reference_width, 4)
                suggestions["entry_zone_basis"] = "reference_width_only"
                issues.append(
                    _issue(
                        "entry_zone_zero_or_narrow",
                        "warning",
                        "entry_zone",
                        "入场区宽度为零或过窄；单点限价本身合法，此项仅提示，不修改数值。",
                        observed=round(high - low, 4),
                        reference=round(reference_width, 4),
                    )
                )

    if stop is not None and low is not None and high is not None and not (
        low <= 0 or high <= 0 or low > high
    ):
        if stop > high:
            # G2：止损高于整个入场区，方向性错误，直接判非法。
            min_pct, _max_pct = _reference_stop_bounds(atr_pct)
            suggestions["stop_loss"] = round(low * (1.0 - min_pct / 100.0), 4)
            suggestions["stop_loss_basis"] = "reference_min_distance_only"
            issues.append(
                _issue(
                    "stop_loss_above_entry_zone",
                    "error",
                    "stop_loss",
                    "多头止损高于入场区上沿，必然立即触发。",
                    observed=stop,
                    reference=high,
                )
            )
        elif stop >= low:
            # G3：止损不低于可成交价（下沿限价），方向性错误。
            min_pct, _max_pct = _reference_stop_bounds(atr_pct)
            suggestions["stop_loss"] = round(low * (1.0 - min_pct / 100.0), 4)
            suggestions["stop_loss_basis"] = "reference_min_distance_only"
            issues.append(
                _issue(
                    "stop_loss_at_or_above_entry_low",
                    "error",
                    "stop_loss",
                    "多头止损不低于入场区下沿（可成交价），买入即触发；请回到模型重算，校验器不代填。",
                    observed=stop,
                    reference=low,
                )
            )
        else:
            distance_pct = (low - stop) / low * 100.0
            min_pct, max_pct = _reference_stop_bounds(atr_pct)
            out_of_reference = distance_pct < min_pct or distance_pct > max_pct
            if out_of_reference:
                target_pct = min_pct if distance_pct < min_pct else max_pct
                suggestions["stop_loss"] = round(low * (1.0 - target_pct / 100.0), 4)
                suggestions["stop_loss_basis"] = "reference_distance_only"
                issues.append(
                    _issue(
                        "stop_loss_distance_out_of_reference_range",
                        "warning",
                        "stop_loss",
                        "止损距离不在参考区间内；仅提示，不修改数值。",
                        observed=round(distance_pct, 4),
                        reference={"min_pct": round(min_pct, 4), "max_pct": round(max_pct, 4)},
                    )
                )
            if atr_pct is None:
                issues.append(
                    _issue(
                        "atr_unavailable",
                        "info",
                        "atr20",
                        "缺少 ATR20，止损距离只能按百分比参考值提示。",
                    )
                )

    status = "ok"
    if any(item["code"] in BLOCKING_CODES for item in issues):
        status = "invalid"
    elif any(item["code"] in REVIEW_CODES for item in issues):
        status = "needs_review"

    return {
        "status": status,
        "issues": issues,
        "suggestions": suggestions,
        "observed": {
            "entry_zone_low": low,
            "entry_zone_high": high,
            "stop_loss_price": stop,
        },
        "atr20_pct": round(atr_pct, 4) if atr_pct is not None else None,
        "advisory_only": True,
    }


def validate_plan_fields(
    plan: Dict[str, Any],
    *,
    entry_field: str,
    stop_field: str,
    atr_pct: Optional[float] = None,
) -> Dict[str, Any]:
    """读取计划字段并校验；不修改 ``plan``。"""
    return validate_price_plan(
        entry_text=plan.get(entry_field),
        stop_text=plan.get(stop_field),
        entry_low=plan.get("entry_zone_low"),
        entry_high=plan.get("entry_zone_high"),
        stop_loss=plan.get("stop_loss_price"),
        atr_pct=atr_pct,
    )
