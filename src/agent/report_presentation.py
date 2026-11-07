# -*- coding: utf-8 -*-
"""Pure, dependency-light presentation helpers for planning reports."""

from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional


_FINAL_OUTPUT_STEP_TITLE_RE = re.compile(r"(?m)^(第[一二三四五六七八九十]+(?:步|阶段))[:：][ \t]*")
def normalize_planning_execute_final_content(content: str) -> str:
    """Return user-facing Markdown and keep internal JSON out of the report body."""
    if not content:
        return content
    normalized = _strip_fundamental_supplement(
        _FINAL_OUTPUT_STEP_TITLE_RE.sub("", content).strip()
    )
    fenced = _leading_json_fence(normalized)
    if fenced is not None:
        json_text, fence_end = fenced
        payload = parse_internal_report_payload(json_text)
        suffix = normalized[fence_end:].lstrip()
        suffix = re.sub(r"^---\s*", "", suffix).lstrip()
        if re.search(r"(?m)^#{2,4}\s+\S+", suffix):
            return suffix
        if payload is not None:
            return render_internal_report_payload(payload)
    embedded_payload = _embedded_dashboard_payload(normalized)
    if embedded_payload is not None:
        return render_internal_report_payload(embedded_payload)
    if normalized.startswith("{") and normalized.endswith("}"):
        payload = parse_internal_report_payload(normalized)
        if payload is not None:
            return render_internal_report_payload(payload)
    return normalized


def _embedded_dashboard_payload(content: str) -> Optional[Dict[str, Any]]:
    """Extract the internal dashboard contract even after introductory prose."""
    markers = ("## \u51b3\u7b56\u4eea\u8868\u76d8 JSON", "## Decision Dashboard JSON")
    marker_positions = [content.find(marker) for marker in markers]
    marker_positions = [position for position in marker_positions if position >= 0]
    if not marker_positions:
        return None
    marker_start = min(marker_positions)
    fence_start = content.find("```json", marker_start)
    if fence_start < 0:
        return None
    body_start = fence_start + len("```json")
    fence_end = content.find("```", body_start)
    if fence_end < 0:
        return None
    return parse_internal_report_payload(content[body_start:fence_end].strip())


def _strip_fundamental_supplement(content: str) -> str:
    """Hide the retired search appendix while preserving immutable Trace files."""
    lines = (content or "").splitlines()
    start = next(
        (
            index
            for index, line in enumerate(lines)
            if re.fullmatch(r"##\s+基本面补充资料\s*", line.strip())
        ),
        None,
    )
    if start is None:
        return content
    end = len(lines)
    for index in range(start + 1, len(lines)):
        stripped = lines[index].strip()
        if stripped == "<!-- deterministic-price-plan:start -->" or re.match(
            r"^##\s+\S+", stripped
        ):
            end = index
            break
    kept = [*lines[:start], *lines[end:]]
    return "\n".join(kept).strip()


def _leading_json_fence(content: str) -> Optional[tuple[str, int]]:
    """Locate an introductory JSON fence in linear time."""
    fence_start = content.find("```json")
    if fence_start < 0:
        return None
    prefix = content[:fence_start]
    if re.search(r"(?m)^#{1,6}\s+", prefix):
        return None
    fence_body_start = fence_start + len("```json")
    fence_end = content.find("```", fence_body_start)
    if fence_end < 0:
        return None
    return content[fence_body_start:fence_end].strip(), fence_end + 3


def parse_internal_report_payload(value: str) -> Optional[Dict[str, Any]]:
    try:
        payload = json.loads(value)
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def render_internal_report_payload(payload: Dict[str, Any]) -> str:
    """Render the model's internal report contract as concise, readable Markdown."""
    action = str(payload.get("action") or payload.get("current_action") or "").strip().lower()
    action_labels = {
        "open": "可以考虑入场",
        "buy": "可以买入",
        "add": "可以考虑加仓",
        "hold": "继续持有",
        "wait": "暂时等待",
        "monitor": "继续观察",
        "reject": "暂不入场",
        "reduce": "考虑减仓",
        "sell": "考虑卖出",
    }
    summary = str(payload.get("summary") or payload.get("analysis_summary") or "暂无可用结论。").strip()
    lines = [
        "## 最终结论",
        "",
        f"**当前建议：{action_labels.get(action, action or '谨慎观察')}。**",
        "",
        summary,
        "",
        "## 多维证据",
        "",
    ]
    evidence = payload.get("evidence") if isinstance(payload.get("evidence"), dict) else {}
    dimension_labels = {
        "technical": "技术面",
        "capital": "资金面",
        "capital_flow": "资金面",
        "fundamental": "基本面",
        "news": "消息面",
        "news_event": "消息面",
        "portfolio": "账户与持仓",
        "data_quality": "数据质量",
    }
    deterministic_score = evidence.get("deterministic_score")
    rendered_dimensions = 0
    for key, value in evidence.items():
        if key == "deterministic_score":
            continue
        items = _report_value_lines(value)
        if not items:
            continue
        rendered_dimensions += 1
        lines.extend([f"### {dimension_labels.get(str(key), str(key))}", ""])
        lines.extend(item if item.startswith("- ") else f"- {item}" for item in items)
        lines.append("")
    if rendered_dimensions == 0:
        lines.extend(["本轮没有形成可展示的多维证据。", ""])

    lines.extend(["## 交易计划", ""])
    plan_items = _report_mapping_lines(payload.get("plan"))
    lines.extend(plan_items or ["- 具体价格由确定性价格引擎单独生成，本段不使用模型临场价格。"])
    lines.extend(["", "## 策略 4 / 缠论确定性评分", ""])
    score_items = _report_mapping_lines(deterministic_score)
    lines.extend(score_items or ["- 本轮内部结果未提供可展示的确定性评分，最终以系统评分模块为准。"])
    lines.extend(["", "## 风险与缺失", ""])
    risk_items = _report_value_lines(payload.get("risks"))
    missing_items = _report_value_lines(payload.get("missing_information"))
    if risk_items:
        lines.extend(["### 主要风险", ""])
        lines.extend(item if item.startswith("- ") else f"- {item}" for item in risk_items)
        lines.append("")
    if missing_items:
        lines.extend(["### 仍需补充", ""])
        lines.extend(item if item.startswith("- ") else f"- {item}" for item in missing_items)
    if not risk_items and not missing_items:
        lines.append("- 暂无额外风险或缺失信息说明。")
    return "\n".join(lines).strip()


def planning_report_quality_issues(
    content: str,
    *,
    report_language: str,
    tool_calls: Optional[List[Dict[str, Any]]] = None,
) -> List[str]:
    """Return presentation and evidence-consistency problems for a report."""
    text = (content or "").strip()
    compact = re.sub(r"\s+", "", text)
    headings = re.findall(r"(?m)^#{2,4}\s+\S+", text)
    issues: List[str] = []
    if len(compact) < 120:
        issues.append("report_too_short")
    if len(headings) < 3:
        issues.append("report_missing_structure")
    lowered = text.lower()
    if report_language == "en":
        required = {
            "report_missing_trade_plan": ("trade plan", "deterministic price", "price plan"),
            "report_missing_score_section": ("strategy 4", "chan", "deterministic score"),
            "report_missing_risk_section": ("risk", "missing evidence", "invalidation"),
        }
    else:
        required = {
            "report_missing_trade_plan": ("交易计划", "确定性价格", "价格计划"),
            "report_missing_score_section": ("策略 4", "策略4", "缠论", "确定性评分"),
            "report_missing_risk_section": ("风险", "缺失", "失效条件"),
        }
    for issue, terms in required.items():
        if not any(term.lower() in lowered for term in terms):
            issues.append(issue)
    if tool_calls:
        from src.agent.dimension_evidence import build_dimension_evidence_summary

        coverage = build_dimension_evidence_summary(tool_calls)
        if coverage["fundamental"]["quality"] != "unavailable" and _claims_dimension_missing(
            text, report_language=report_language, dimension="fundamental"
        ):
            issues.append("report_contradicts_fundamental_evidence")
        if coverage["intel"]["quality"] != "unavailable" and _claims_dimension_missing(
            text, report_language=report_language, dimension="intel"
        ):
            issues.append("report_contradicts_news_evidence")
    return issues


def _report_value_lines(value: Any) -> List[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, dict):
        return _report_mapping_lines(value)
    text = str(value or "").strip()
    return [text] if text else []


def _report_mapping_lines(value: Any) -> List[str]:
    if not isinstance(value, dict):
        return _report_value_lines(value) if value not in (None, "") else []
    key_labels = {
        "entry": "入场",
        "exit": "退出",
        "stop_loss": "止损",
        "position_size": "仓位",
        "conditions": "条件",
        "status": "状态",
        "score": "得分",
        "reason": "原因",
    }
    lines: List[str] = []
    for key, item in value.items():
        label = key_labels.get(str(key), str(key))
        if isinstance(item, list):
            clean = [str(entry).strip() for entry in item if str(entry).strip()]
            if clean:
                lines.append(f"{label}：{'；'.join(clean)}")
        elif isinstance(item, dict):
            nested = _report_mapping_lines(item)
            if nested:
                lines.append(f"{label}：{'；'.join(nested)}")
        elif item not in (None, ""):
            lines.append(f"{label}：{item}")
    return [f"- {line}" if not line.startswith("-") else line for line in lines]


def _claims_dimension_missing(text: str, *, report_language: str, dimension: str) -> bool:
    compact = re.sub(r"[`*_~\s]+", "", text).lower()
    if report_language == "en":
        markers = {
            "fundamental": ("fundamentalhealth:0/10", "fundamentalscore:0", "fundamentalscompletelymissing", "nofundamentalevidence"),
            "intel": ("newsclarity:0/10", "newsscore:0", "newsevidencecompletelymissing", "noneffectiveinformation"),
        }
    else:
        markers = {
            "fundamental": ("基本面健全度：0/10", "基本面健全度:0/10", "基本面评分：0", "基本面评分:0", "基本面完全缺失"),
            "intel": ("消息面明确性：0/10", "消息面明确性:0/10", "消息面评分：0", "消息面评分:0", "消息面完全缺失", "无有效信息"),
        }
    return any(marker.lower().replace(" ", "") in compact for marker in markers.get(dimension, ()))
