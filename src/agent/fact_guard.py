# -*- coding: utf-8 -*-
"""Guard final Agent dashboards against pseudo-precise financial facts."""

from __future__ import annotations

import re
from copy import deepcopy
from typing import Any, Dict, List, Tuple


_A_SHARE_RE = re.compile(r"^(?:SH|SZ)?\d{6}(?:\.(?:SH|SZ|BJ))?$", re.IGNORECASE)
_INTEGER_RE = re.compile(r"(?<![\d.])(\d{1,9})(?:\.0+)?\s*股")
_CHIP_KEYS = ("profit_ratio", "avg_cost", "concentration", "cost_90_low", "cost_90_high")
_PRICE_KEYS = ("ma5", "ma10", "ma20")
_CAPITAL_KEYS = (
    "main_net_inflow",
    "main_inflow_5d",
    "main_inflow_10d",
    "inflow_5d",
    "inflow_10d",
    "net_inflow",
    "net_inflow_5d",
    "net_inflow_10d",
)


def sanitize_dashboard_facts(dashboard: Dict[str, Any], ctx: Any) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    """Return a copy of ``dashboard`` with unverified precision removed.

    LLMs often produce professional-looking numbers when the corresponding
    tool failed or was never called. This guard only permits exact capital
    flow, chip, and MA values when the current AgentContext contains a matching
    successful tool payload. It also enforces A-share buy-lot rules in text.
    """

    cleaned = deepcopy(dashboard) if isinstance(dashboard, dict) else {}
    warnings: List[Dict[str, Any]] = []
    dash = cleaned.get("dashboard")
    if not isinstance(dash, dict):
        return cleaned, warnings

    data_perspective = dash.get("data_perspective")
    if isinstance(data_perspective, dict):
        _sanitize_price_position(data_perspective, ctx, warnings)
        _sanitize_chip_structure(data_perspective, ctx, warnings)
        _sanitize_capital_flow(data_perspective, ctx, warnings)

    _sanitize_buy_lot_text(dash, getattr(ctx, "stock_code", ""), warnings)
    if warnings:
        existing = dash.get("data_quality_warnings")
        if not isinstance(existing, list):
            existing = []
        dash["data_quality_warnings"] = existing + warnings
    return cleaned, warnings


def _sanitize_price_position(data_perspective: Dict[str, Any], ctx: Any, warnings: List[Dict[str, Any]]) -> None:
    price_position = data_perspective.get("price_position")
    if not isinstance(price_position, dict):
        return
    verified = _verified_ma_values(ctx)
    for key in _PRICE_KEYS:
        value = price_position.get(key)
        if _is_placeholder(value):
            continue
        if key in verified and _numbers_equal(value, verified[key]):
            continue
        price_position[key] = "N/A"
        _warn_once(warnings, "unverified_ma", f"{key} 缺少 calculate_ma/analyze_trend 工具证据，已移除模型给出的精确值。", {"field": key})


def _sanitize_chip_structure(data_perspective: Dict[str, Any], ctx: Any, warnings: List[Dict[str, Any]]) -> None:
    chip_structure = data_perspective.get("chip_structure")
    if not isinstance(chip_structure, dict):
        return
    verified = _verified_chip_values(ctx)
    for key in _CHIP_KEYS:
        if key not in chip_structure or _is_placeholder(chip_structure.get(key)):
            continue
        if key in verified and _values_match(chip_structure.get(key), verified[key]):
            continue
        chip_structure[key] = "N/A"
        _warn_once(warnings, "unverified_chip", f"{key} 缺少 get_chip_distribution 工具证据，已移除模型给出的精确筹码值。", {"field": key})
    if "chip_health" in chip_structure and not verified:
        chip_structure["chip_health"] = "N/A"


def _sanitize_capital_flow(data_perspective: Dict[str, Any], ctx: Any, warnings: List[Dict[str, Any]]) -> None:
    capital_flow = data_perspective.get("capital_flow")
    if not isinstance(capital_flow, dict):
        return
    verified = _verified_capital_values(ctx)
    for key in _CAPITAL_KEYS:
        if key not in capital_flow or _is_placeholder(capital_flow.get(key)):
            continue
        if key in verified and _numbers_equal(capital_flow.get(key), verified[key]):
            continue
        capital_flow[key] = "N/A"
        _warn_once(warnings, "unverified_capital_flow", f"{key} 缺少 get_capital_flow 工具证据，已移除模型给出的精确资金流数值。", {"field": key})


def _sanitize_buy_lot_text(dashboard_block: Dict[str, Any], stock_code: str, warnings: List[Dict[str, Any]]) -> None:
    if not _is_a_share(stock_code):
        return

    def visit(obj: Any) -> Any:
        if isinstance(obj, dict):
            return {key: visit(value) for key, value in obj.items()}
        if isinstance(obj, list):
            return [visit(item) for item in obj]
        if isinstance(obj, str):
            return _sanitize_buy_lot_string(obj, warnings)
        return obj

    for key in ("core_conclusion", "battle_plan"):
        if key in dashboard_block:
            dashboard_block[key] = visit(dashboard_block[key])


def _sanitize_buy_lot_string(text: str, warnings: List[Dict[str, Any]]) -> str:
    def replace(match: re.Match[str]) -> str:
        qty = int(match.group(1))
        if qty % 100 == 0:
            return match.group(0)
        _warn_once(
            warnings,
            "invalid_a_share_buy_lot",
            "A 股买入必须按 100 股整数倍下单，已移除模型给出的非法买入股数。",
            {"quantity": qty},
        )
        return "按100股整数倍"

    return _INTEGER_RE.sub(replace, text)


def _verified_ma_values(ctx: Any) -> Dict[str, float]:
    verified: Dict[str, float] = {}
    for source in (_ctx_data(ctx, "trend_result"), _latest_raw_data(ctx, {"technical"})):
        if not isinstance(source, dict):
            continue
        for key in _PRICE_KEYS:
            value = _to_float(source.get(key))
            if value is not None:
                verified[key] = value
        ma = source.get("ma")
        if isinstance(ma, dict):
            for period in (5, 10, 20):
                item = ma.get(f"ma{period}")
                if isinstance(item, dict):
                    value = _to_float(item.get("value"))
                else:
                    value = _to_float(item)
                if value is not None:
                    verified[f"ma{period}"] = value
    return verified


def _verified_chip_values(ctx: Any) -> Dict[str, Any]:
    chip = _ctx_data(ctx, "chip_distribution")
    if not isinstance(chip, dict) or chip.get("status") not in (None, "ok"):
        return {}
    source_chain = chip.get("source_chain")
    if source_chain is not None and not _has_ok_source(source_chain):
        return {}
    mapping = {
        "profit_ratio": chip.get("profit_ratio"),
        "avg_cost": chip.get("avg_cost"),
        "concentration": chip.get("concentration_90") if chip.get("concentration_90") is not None else chip.get("concentration"),
        "cost_90_low": chip.get("cost_90_low"),
        "cost_90_high": chip.get("cost_90_high"),
    }
    return {key: value for key, value in mapping.items() if value not in (None, "", [], {})}


def _verified_capital_values(ctx: Any) -> Dict[str, Any]:
    capital = _ctx_data(ctx, "capital_flow")
    if not isinstance(capital, dict) or capital.get("status") not in (None, "ok", "partial"):
        return {}
    source_chain = capital.get("source_chain")
    if not _has_ok_source(source_chain):
        return {}
    return {key: capital.get(key) for key in _CAPITAL_KEYS if capital.get(key) not in (None, "", [], {})}


def _ctx_data(ctx: Any, key: str) -> Any:
    getter = getattr(ctx, "get_data", None)
    if callable(getter):
        return getter(key)
    data = getattr(ctx, "data", None)
    return data.get(key) if isinstance(data, dict) else None


def _latest_raw_data(ctx: Any, names: set[str]) -> Dict[str, Any]:
    for opinion in reversed(getattr(ctx, "opinions", []) or []):
        if getattr(opinion, "agent_name", None) in names and isinstance(getattr(opinion, "raw_data", None), dict):
            return opinion.raw_data
    return {}


def _has_ok_source(source_chain: Any) -> bool:
    if not isinstance(source_chain, list):
        return False
    for item in source_chain:
        if not isinstance(item, dict):
            continue
        result = str(item.get("result") or item.get("status") or "").lower()
        if result == "ok":
            return True
    return False


def _values_match(display_value: Any, source_value: Any) -> bool:
    source_num = _to_float(source_value)
    display_num = _to_float(display_value)
    if source_num is not None and display_num is not None:
        if "%" in str(display_value) and abs(source_num) <= 1:
            source_num *= 100
        return abs(display_num - source_num) <= max(0.01, abs(source_num) * 0.001)
    return str(display_value).strip() == str(source_value).strip()


def _numbers_equal(left: Any, right: Any) -> bool:
    left_num = _to_float(left)
    right_num = _to_float(right)
    if left_num is None or right_num is None:
        return False
    return abs(left_num - right_num) <= max(0.01, abs(right_num) * 0.001)


def _to_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        text = str(value).replace(",", "").replace("%", "").strip()
        if not text or text.lower() in {"nan", "n/a", "na", "none"}:
            return None
        return float(text)
    except (TypeError, ValueError):
        return None


def _is_placeholder(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        return value.strip().lower() in {"", "n/a", "na", "none", "unknown", "数据缺失", "未知", "待补充"}
    return False


def _is_a_share(stock_code: str) -> bool:
    code = str(stock_code or "").strip()
    if not _A_SHARE_RE.match(code):
        return False
    digits = "".join(ch for ch in code if ch.isdigit())
    return len(digits) == 6 and digits[0] in "0346689"


def _warn_once(warnings: List[Dict[str, Any]], code: str, message: str, details: Dict[str, Any]) -> None:
    if any(item.get("code") == code and item.get("details") == details for item in warnings):
        return
    warnings.append({"code": code, "message": message, "details": details})
