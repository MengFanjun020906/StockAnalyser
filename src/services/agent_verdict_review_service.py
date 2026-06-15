# -*- coding: utf-8 -*-
"""Offline verdict review builder for Agent Trace artifacts.

The service intentionally stays read-only for Agent traces and market data. It
turns past Judge decisions into review rows that can later support calibration,
without injecting any insight back into live prompts.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence

from src.config import get_config
from src.core.backtest_engine import BacktestEngine, EvaluationConfig
from src.repositories.stock_repo import StockRepository
from src.storage import DatabaseManager


DEFAULT_EVAL_WINDOWS = (7, 30)
KNOWN_REVIEW_LABELS = ("hit", "missed_up", "avoided_down", "wrong_direction", "no_edge", "neutral_ok", "insufficient_data", "unclassified")
KNOWN_CHAIN_TYPES = ("stock_selection", "single_stock_analysis")


@dataclass(frozen=True)
class VerdictReviewBuildResult:
    """Summary returned by verdict review builds."""

    trace_count: int
    review_count: int
    output_path: Optional[str] = None
    skipped: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "trace_count": self.trace_count,
            "review_count": self.review_count,
            "output_path": self.output_path,
            "skipped": self.skipped,
        }


@dataclass(frozen=True)
class VerdictReviewInsightResult:
    """Summary returned by offline insight markdown builds."""

    row_count: int
    completed_count: int
    group_count: int
    stable_insight_count: int
    output_path: Optional[str] = None
    min_samples: int = 20

    def to_dict(self) -> Dict[str, Any]:
        return {
            "row_count": self.row_count,
            "completed_count": self.completed_count,
            "group_count": self.group_count,
            "stable_insight_count": self.stable_insight_count,
            "output_path": self.output_path,
            "min_samples": self.min_samples,
        }


class AgentVerdictReviewService:
    """Build post-hoc review JSONL rows from existing Agent Trace artifacts."""

    def __init__(
        self,
        *,
        db_manager: Optional[DatabaseManager] = None,
        stock_repo: Optional[StockRepository] = None,
    ) -> None:
        self.db = db_manager or DatabaseManager.get_instance()
        self.stock_repo = stock_repo or StockRepository(self.db)

    @staticmethod
    def default_trace_root() -> Path:
        return Path(get_config().database_path).expanduser().resolve().parent / "agent_traces"

    @staticmethod
    def default_output_path() -> Path:
        return Path("data/agent_reviews/verdict_review.jsonl")

    @staticmethod
    def default_insight_output_path() -> Path:
        return Path("data/agent_reviews/insights/agent_verdict_insights.md")

    def build_reviews(
        self,
        *,
        trace_root: Optional[Path] = None,
        eval_windows: Sequence[int] = DEFAULT_EVAL_WINDOWS,
        limit: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        root = Path(trace_root) if trace_root is not None else self.default_trace_root()
        windows = _normalize_windows(eval_windows)
        reviews: List[Dict[str, Any]] = []
        for trace_dir in _iter_trace_dirs(root, limit=limit):
            reviews.extend(self.build_reviews_for_trace(trace_dir=trace_dir, eval_windows=windows))
        return reviews

    def build_reviews_for_trace(
        self,
        *,
        trace_dir: Path,
        eval_windows: Sequence[int] = DEFAULT_EVAL_WINDOWS,
    ) -> List[Dict[str, Any]]:
        final_report = _read_json(trace_dir / "final_report.json")
        if not final_report:
            stock_selection = _read_json(trace_dir / "stock_selection.json")
            final_report = stock_selection.get("final_report_json") if isinstance(stock_selection, dict) else {}
        if not isinstance(final_report, dict) or not final_report:
            return []
        if not _is_stock_selection_report(final_report):
            single_stock = self._build_single_stock_review(
                trace_dir=trace_dir,
                final_report=final_report,
                eval_windows=eval_windows,
            )
            return [single_stock] if single_stock else []

        request = _read_json(trace_dir / "request.json")
        summary = _read_json(trace_dir / "summary.json")
        decision_date = _resolve_decision_date(final_report, trace_dir)
        judge_summary = _nested_dict(final_report, "judge_decision", "summary")
        allocation_full = _nested_dict(final_report, "portfolio_allocation", "full")
        positions = allocation_full.get("positions_plan") if isinstance(allocation_full.get("positions_plan"), list) else []
        market_regime = final_report.get("market_regime") if isinstance(final_report.get("market_regime"), dict) else {}
        symbols = _extract_review_symbols(final_report)
        windows = _normalize_windows(eval_windows)

        rows: List[Dict[str, Any]] = []
        for symbol_info in symbols:
            symbol = symbol_info.get("code")
            if not symbol:
                continue
            plan = _plan_for_symbol(positions, symbol)
            action = _resolve_symbol_action(plan, judge_summary)
            evaluation = self._evaluate_symbol(
                symbol=symbol,
                decision_date=decision_date,
                action=action,
                windows=windows,
            )
            rows.append({
                "schema_version": "agent_verdict_review.v1",
                "chain_type": "stock_selection",
                "trace_id": _trace_id(trace_dir, summary),
                "trace_dir": str(trace_dir),
                "decision_date": decision_date.isoformat(),
                "symbol": symbol,
                "name": symbol_info.get("name") or plan.get("name"),
                "intent": _resolve_intent(request, final_report),
                "final_action": judge_summary.get("final_action"),
                "primary_plan_verdict": judge_summary.get("primary_plan_verdict"),
                "symbol_action": action,
                "confidence": _resolve_confidence(judge_summary, plan),
                "regime": market_regime.get("regime"),
                "risk_level": market_regime.get("risk_level"),
                "data_quality": _resolve_data_quality(market_regime, evaluation),
                "start_price": evaluation.get("start_price"),
                "start_date": evaluation.get("start_date"),
                "windows": evaluation.get("windows", {}),
                "review_label": _classify_review_label(action=action, evaluation=evaluation),
                "limits": evaluation.get("limits", []),
            })
        return rows

    def _build_single_stock_review(
        self,
        *,
        trace_dir: Path,
        final_report: Dict[str, Any],
        eval_windows: Sequence[int],
    ) -> Optional[Dict[str, Any]]:
        request = _read_json(trace_dir / "request.json")
        context = _read_json(trace_dir / "context.json")
        summary = _read_json(trace_dir / "summary.json")
        risk_gate = _read_json(trace_dir / "risk_gate.json")
        symbol = _resolve_single_stock_symbol(
            final_report=final_report,
            request=request,
            context=context,
            risk_gate=risk_gate,
        )
        if not symbol:
            return None
        decision_date = _resolve_single_stock_decision_date(final_report, trace_dir)
        action = _resolve_single_stock_action(final_report, risk_gate)
        evaluation = self._evaluate_symbol(
            symbol=symbol,
            decision_date=decision_date,
            action=action,
            windows=_normalize_windows(eval_windows),
        )
        return {
            "schema_version": "agent_verdict_review.v1",
            "chain_type": "single_stock_analysis",
            "trace_id": _trace_id(trace_dir, summary),
            "trace_dir": str(trace_dir),
            "decision_date": decision_date.isoformat(),
            "symbol": symbol,
            "name": _resolve_single_stock_name(final_report, request, context),
            "intent": _resolve_single_stock_intent(request, context),
            "final_action": action,
            "primary_plan_verdict": None,
            "symbol_action": action,
            "operation_advice": final_report.get("operation_advice"),
            "decision_type": final_report.get("decision_type"),
            "confidence": _resolve_single_stock_confidence(final_report),
            "regime": None,
            "risk_level": None,
            "data_quality": _resolve_data_quality({}, evaluation),
            "start_price": evaluation.get("start_price"),
            "start_date": evaluation.get("start_date"),
            "windows": evaluation.get("windows", {}),
            "review_label": _classify_review_label(action=action, evaluation=evaluation),
            "limits": evaluation.get("limits", []),
        }

    def write_reviews(
        self,
        reviews: Iterable[Dict[str, Any]],
        *,
        output_path: Optional[Path] = None,
    ) -> VerdictReviewBuildResult:
        rows = list(reviews)
        path = Path(output_path) if output_path is not None else self.default_output_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        with tmp.open("w", encoding="utf-8") as fh:
            for row in rows:
                fh.write(json.dumps(row, ensure_ascii=False, sort_keys=True, default=str))
                fh.write("\n")
        tmp.replace(path)
        return VerdictReviewBuildResult(
            trace_count=len({row.get("trace_id") for row in rows}),
            review_count=len(rows),
            output_path=str(path),
            skipped=0,
        )

    def build_and_write(
        self,
        *,
        trace_root: Optional[Path] = None,
        output_path: Optional[Path] = None,
        eval_windows: Sequence[int] = DEFAULT_EVAL_WINDOWS,
        limit: Optional[int] = None,
    ) -> VerdictReviewBuildResult:
        root = Path(trace_root) if trace_root is not None else self.default_trace_root()
        trace_dirs = list(_iter_trace_dirs(root, limit=limit))
        reviews: List[Dict[str, Any]] = []
        for trace_dir in trace_dirs:
            reviews.extend(self.build_reviews_for_trace(trace_dir=trace_dir, eval_windows=eval_windows))
        result = self.write_reviews(reviews, output_path=output_path)
        return VerdictReviewBuildResult(
            trace_count=len(trace_dirs),
            review_count=result.review_count,
            output_path=result.output_path,
            skipped=max(0, len(trace_dirs) - len({row.get("trace_id") for row in reviews})),
        )

    def query_reviews(
        self,
        *,
        input_path: Optional[Path] = None,
        chain_type: Optional[str] = None,
        review_label: Optional[str] = None,
        symbol: Optional[str] = None,
        limit: int = 200,
    ) -> Dict[str, Any]:
        path = Path(input_path) if input_path is not None else self.default_output_path()
        rows = _read_review_jsonl(path)
        filtered = _filter_review_rows(
            rows,
            chain_type=chain_type,
            review_label=review_label,
            symbol=symbol,
        )
        filtered.sort(key=lambda item: (str(item.get("decision_date") or ""), str(item.get("trace_id") or "")), reverse=True)
        safe_limit = max(1, min(int(limit or 200), 1000))
        return {
            "source_path": str(path),
            "exists": path.exists(),
            "total": len(filtered),
            "items": filtered[:safe_limit],
            "summary": _summarize_review_rows(filtered),
        }

    def build_insight_markdown(
        self,
        *,
        input_path: Optional[Path] = None,
        output_path: Optional[Path] = None,
        min_samples: int = 20,
        top_n: int = 12,
    ) -> VerdictReviewInsightResult:
        """Generate deterministic offline insight markdown from review JSONL.

        The generated file is intentionally not wired into live Agent prompts.
        It is a local review artifact that can be read by humans before any
        future threshold-gated injection is designed.
        """

        source = Path(input_path) if input_path is not None else self.default_output_path()
        target = Path(output_path) if output_path is not None else self.default_insight_output_path()
        rows = _read_review_jsonl(source)
        safe_min_samples = max(1, int(min_samples or 20))
        safe_top_n = max(1, min(int(top_n or 12), 50))
        groups = _build_insight_groups(rows, min_samples=safe_min_samples)
        stable = [group for group in groups if group.get("insight")]
        completed_count = sum(1 for row in rows if _preferred_review_window(row).get("eval_status") == "completed")
        markdown = _render_insight_markdown(
            source_path=source,
            rows=rows,
            groups=groups,
            stable=stable,
            min_samples=safe_min_samples,
            top_n=safe_top_n,
        )
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(target.suffix + ".tmp")
        tmp.write_text(markdown, encoding="utf-8")
        tmp.replace(target)
        return VerdictReviewInsightResult(
            row_count=len(rows),
            completed_count=completed_count,
            group_count=len(groups),
            stable_insight_count=len(stable),
            output_path=str(target),
            min_samples=safe_min_samples,
        )

    def _evaluate_symbol(
        self,
        *,
        symbol: str,
        decision_date: date,
        action: str,
        windows: Sequence[int],
    ) -> Dict[str, Any]:
        code_candidates = _stock_code_candidates(symbol)
        start_bar = None
        start_code = None
        for code in code_candidates:
            start_bar = self.stock_repo.get_start_daily(code=code, analysis_date=decision_date)
            if start_bar is not None:
                start_code = code
                break
        if start_bar is None or not start_bar.close or start_bar.close <= 0:
            return {
                "status": "insufficient_start_price",
                "limits": ["missing_start_bar"],
                "windows": {},
            }

        max_window = max(windows) if windows else 0
        forward_bars = self.stock_repo.get_forward_bars(
            code=start_code or symbol,
            analysis_date=decision_date,
            eval_window_days=max_window,
        )
        results: Dict[str, Any] = {}
        limits: List[str] = []
        for window in windows:
            evaluated = BacktestEngine.evaluate_single(
                operation_advice=_operation_advice_for_action(action),
                analysis_date=decision_date,
                start_price=float(start_bar.close),
                forward_bars=forward_bars[:window],
                stop_loss=None,
                take_profit=None,
                config=EvaluationConfig(eval_window_days=window, neutral_band_pct=2.0, engine_version="agent_verdict_review.v1"),
            )
            status = evaluated.get("eval_status")
            if status != "completed":
                limits.append(f"{window}d:{status}")
            results[str(window)] = {
                "eval_status": status,
                "future_return_pct": _round_or_none(evaluated.get("stock_return_pct")),
                "simulated_return_pct": _round_or_none(evaluated.get("simulated_return_pct")),
                "direction_expected": evaluated.get("direction_expected"),
                "direction_correct": evaluated.get("direction_correct"),
                "outcome": evaluated.get("outcome"),
                "end_close": _round_or_none(evaluated.get("end_close")),
                "max_high": _round_or_none(evaluated.get("max_high")),
                "min_low": _round_or_none(evaluated.get("min_low")),
            }
        return {
            "status": "completed" if not limits else "partial",
            "start_price": _round_or_none(start_bar.close),
            "start_date": start_bar.date.isoformat(),
            "windows": results,
            "limits": limits,
        }


def _iter_trace_dirs(root: Path, *, limit: Optional[int]) -> List[Path]:
    if not root.exists():
        return []
    dirs = [path for path in root.iterdir() if path.is_dir()]
    dirs.sort(key=lambda item: item.name, reverse=True)
    return dirs[:limit] if limit is not None and limit > 0 else dirs


def _read_json(path: Path) -> Dict[str, Any]:
    try:
        if not path.exists():
            return {}
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _read_review_jsonl(path: Path) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    try:
        if not path.exists():
            return rows
        for line in path.read_text(encoding="utf-8").splitlines():
            text = line.strip()
            if not text:
                continue
            try:
                item = json.loads(text)
            except json.JSONDecodeError:
                continue
            if isinstance(item, dict):
                rows.append(item)
    except Exception:
        return []
    return rows


def _filter_review_rows(
    rows: List[Dict[str, Any]],
    *,
    chain_type: Optional[str],
    review_label: Optional[str],
    symbol: Optional[str],
) -> List[Dict[str, Any]]:
    normalized_chain = str(chain_type or "").strip()
    normalized_label = str(review_label or "").strip()
    normalized_symbol = _normalize_symbol(symbol)
    result: List[Dict[str, Any]] = []
    for row in rows:
        if normalized_chain and str(row.get("chain_type") or "") != normalized_chain:
            continue
        if normalized_label and str(row.get("review_label") or "") != normalized_label:
            continue
        if normalized_symbol and _normalize_symbol(row.get("symbol")) != normalized_symbol:
            continue
        result.append(row)
    return result


def _summarize_review_rows(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    by_chain = {key: 0 for key in KNOWN_CHAIN_TYPES}
    by_label = {key: 0 for key in KNOWN_REVIEW_LABELS}
    completed = 0
    future_returns: List[float] = []
    for row in rows:
        chain = str(row.get("chain_type") or "unknown")
        by_chain[chain] = by_chain.get(chain, 0) + 1
        label = str(row.get("review_label") or "unclassified")
        by_label[label] = by_label.get(label, 0) + 1
        preferred = _preferred_review_window(row)
        if preferred.get("eval_status") == "completed":
            completed += 1
        value = preferred.get("future_return_pct")
        if isinstance(value, (int, float)):
            future_returns.append(float(value))
    total = len(rows)
    return {
        "total": total,
        "completed_count": completed,
        "completion_rate_pct": round(completed / total * 100, 2) if total else 0,
        "avg_future_return_pct": round(sum(future_returns) / len(future_returns), 4) if future_returns else None,
        "chain_counts": by_chain,
        "label_counts": by_label,
    }


def _build_insight_groups(rows: List[Dict[str, Any]], *, min_samples: int) -> List[Dict[str, Any]]:
    buckets: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        for dimension, label, value in _insight_dimensions(row):
            key = f"{dimension}:{value}"
            bucket = buckets.setdefault(
                key,
                {
                    "key": key,
                    "dimension": dimension,
                    "label": label,
                    "value": value,
                    "rows": [],
                },
            )
            bucket["rows"].append(row)

    groups: List[Dict[str, Any]] = []
    for bucket in buckets.values():
        stats = _summarize_insight_bucket(bucket["rows"])
        group = {
            "key": bucket["key"],
            "dimension": bucket["dimension"],
            "label": bucket["label"],
            "value": bucket["value"],
            **stats,
        }
        group["insight"] = _infer_stable_insight(group, min_samples=min_samples)
        groups.append(group)

    groups.sort(
        key=lambda item: (
            bool(item.get("insight")),
            int(item.get("completed_count") or 0),
            int(item.get("total") or 0),
            str(item.get("key") or ""),
        ),
        reverse=True,
    )
    return groups


def _insight_dimensions(row: Dict[str, Any]) -> List[tuple[str, str, str]]:
    chain = str(row.get("chain_type") or "unknown").strip() or "unknown"
    action = str(row.get("symbol_action") or row.get("final_action") or "unknown").strip().lower() or "unknown"
    regime = str(row.get("regime") or "").strip()
    dimensions = [
        ("overall", "整体", "all"),
        ("chain_type", "链路", chain),
        ("action", "动作", action),
        ("chain_action", "链路 + 动作", f"{chain}/{action}"),
    ]
    if regime:
        dimensions.append(("regime", "市场状态", regime))
        dimensions.append(("chain_regime", "链路 + 市场状态", f"{chain}/{regime}"))
    return dimensions


def _summarize_insight_bucket(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    label_counts = {key: 0 for key in KNOWN_REVIEW_LABELS}
    completed_count = 0
    future_returns: List[float] = []
    window_counts: Dict[str, int] = {}
    trace_ids = set()
    symbols = set()
    for row in rows:
        trace_id = str(row.get("trace_id") or "").strip()
        symbol = str(row.get("symbol") or "").strip()
        if trace_id:
            trace_ids.add(trace_id)
        if symbol:
            symbols.add(symbol)
        label = str(row.get("review_label") or "unclassified")
        label_counts[label] = label_counts.get(label, 0) + 1
        window_key, preferred = _preferred_review_window_with_key(row)
        if window_key:
            window_counts[window_key] = window_counts.get(window_key, 0) + 1
        if preferred.get("eval_status") == "completed":
            completed_count += 1
            value = preferred.get("future_return_pct")
            if isinstance(value, (int, float)):
                future_returns.append(float(value))
    dominant_label, dominant_count = _dominant_count(label_counts)
    label_denominator = max(len(rows), 1)
    return {
        "total": len(rows),
        "completed_count": completed_count,
        "trace_count": len(trace_ids),
        "symbol_count": len(symbols),
        "completion_rate_pct": round(completed_count / len(rows) * 100, 2) if rows else 0,
        "avg_future_return_pct": round(sum(future_returns) / len(future_returns), 4) if future_returns else None,
        "label_counts": label_counts,
        "dominant_label": dominant_label,
        "dominant_label_count": dominant_count,
        "dominant_label_rate_pct": round(dominant_count / label_denominator * 100, 2) if rows else 0,
        "window_counts": window_counts,
    }


def _infer_stable_insight(group: Dict[str, Any], *, min_samples: int) -> Optional[Dict[str, Any]]:
    completed = int(group.get("completed_count") or 0)
    if completed < min_samples:
        return None
    label_counts = group.get("label_counts") if isinstance(group.get("label_counts"), dict) else {}
    missed_up_rate = _rate(label_counts.get("missed_up"), completed)
    wrong_direction_rate = _rate(label_counts.get("wrong_direction"), completed)
    hit_rate = _rate(label_counts.get("hit"), completed)
    avoided_down_rate = _rate(label_counts.get("avoided_down"), completed)
    avg_return = group.get("avg_future_return_pct")
    avg = float(avg_return) if isinstance(avg_return, (int, float)) else 0.0

    if missed_up_rate >= 40 and avg >= 2:
        return {
            "tone": "risk",
            "title": "防守/等待后踏空样本偏多",
            "detail": "该分组中 missed_up 占比较高，说明等待或拒绝后出现上涨的样本偏多。",
        }
    if wrong_direction_rate >= 30 and avg <= -2:
        return {
            "tone": "risk",
            "title": "主动方向错误样本偏多",
            "detail": "该分组中 wrong_direction 占比较高，说明主动看多或持有后下跌的样本偏多。",
        }
    if hit_rate >= 55 and avg >= 2:
        return {
            "tone": "support",
            "title": "主动判断命中率较高",
            "detail": "该分组中 hit 占比较高，说明同类主动判断在样本内表现较好。",
        }
    if avoided_down_rate >= 40 and avg <= -2:
        return {
            "tone": "support",
            "title": "防守动作有效避险",
            "detail": "该分组中 avoided_down 占比较高，说明等待、减仓或拒绝在样本内减少了下跌暴露。",
        }
    return None


def _render_insight_markdown(
    *,
    source_path: Path,
    rows: List[Dict[str, Any]],
    groups: List[Dict[str, Any]],
    stable: List[Dict[str, Any]],
    min_samples: int,
    top_n: int,
) -> str:
    summary = _summarize_review_rows(rows)
    generated_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    lines = [
        "# Agent Verdict Review Insights",
        "",
        f"- 生成时间：{generated_at}",
        f"- 数据源：`{source_path}`",
        f"- 样本数：{len(rows)}",
        f"- 已完成窗口样本：{summary.get('completed_count', 0)}",
        f"- 稳定洞察阈值：同一分组至少 {min_samples} 条 completed 样本",
        "- 使用边界：本文件仅用于离线复盘，不自动注入线上 Agent、Meta-Agent 或 Judge。",
        "- 数据边界：只基于本地 Trace 与本地 `StockDaily` 生成的 `verdict_review.jsonl`，不拉取外部行情。",
        "",
        "## 稳定洞察",
        "",
    ]
    if stable:
        lines.extend([
            "| 分组 | Completed | 平均后验收益 | 主标签 | 洞察 | 适用边界 |",
            "| --- | ---: | ---: | --- | --- | --- |",
        ])
        for group in stable[:top_n]:
            insight = group.get("insight") if isinstance(group.get("insight"), dict) else {}
            lines.append(
                "| {group} | {completed} | {avg} | {label} | {title} | {boundary} |".format(
                    group=_markdown_cell(f"{group.get('label')}: {group.get('value')}"),
                    completed=group.get("completed_count", 0),
                    avg=_markdown_cell(_fmt_pct(group.get("avg_future_return_pct"))),
                    label=_markdown_cell(_label_with_rate(group)),
                    title=_markdown_cell(str(insight.get("title") or "")),
                    boundary=_markdown_cell("仅适用于同分组、同复盘窗口；样本外需人工确认。"),
                )
            )
    else:
        lines.append("暂无达到阈值的稳定洞察；当前样本只适合人工查看，不适合沉淀为长期提示。")

    lines.extend([
        "",
        "## 样本概览",
        "",
        "| 分组 | Total | Completed | 完成率 | 平均后验收益 | 主标签 |",
        "| --- | ---: | ---: | ---: | ---: | --- |",
    ])
    for group in groups[:top_n]:
        lines.append(
            "| {group} | {total} | {completed} | {completion} | {avg} | {label} |".format(
                group=_markdown_cell(f"{group.get('label')}: {group.get('value')}"),
                total=group.get("total", 0),
                completed=group.get("completed_count", 0),
                completion=_markdown_cell(_fmt_pct(group.get("completion_rate_pct"))),
                avg=_markdown_cell(_fmt_pct(group.get("avg_future_return_pct"))),
                label=_markdown_cell(_label_with_rate(group)),
            )
        )

    lines.extend([
        "",
        "## 标签分布",
        "",
        "| 标签 | 数量 |",
        "| --- | ---: |",
    ])
    label_counts = summary.get("label_counts") if isinstance(summary.get("label_counts"), dict) else {}
    for label, count in sorted(label_counts.items(), key=lambda item: (-int(item[1] or 0), item[0])):
        if int(count or 0) <= 0:
            continue
        lines.append(f"| {_markdown_cell(label)} | {count} |")

    lines.extend([
        "",
        "## 后续使用规则",
        "",
        "- 样本不足、缺少起始价或未来行情不足时，只能保留 `insufficient_data` / `partial`，不能强行归因。",
        "- 若未来注入 Meta-Agent 或 Judge，必须另设阈值门、灰度开关和 trace_id 追溯，不得直接读取本文件作为强规则。",
        "- 黑天鹅、停牌、涨跌停不可成交等样本需要单独标注后再参与归因。",
        "",
    ])
    return "\n".join(lines)


def _preferred_review_window_with_key(row: Dict[str, Any]) -> tuple[Optional[str], Dict[str, Any]]:
    windows = row.get("windows") if isinstance(row.get("windows"), dict) else {}
    for key in ("30", "7"):
        value = windows.get(key)
        if isinstance(value, dict):
            return key, value
    for key, value in windows.items():
        if isinstance(value, dict) and value.get("eval_status") == "completed":
            return str(key), value
    return None, {}


def _dominant_count(counts: Dict[str, int]) -> tuple[str, int]:
    if not counts:
        return "unclassified", 0
    label, count = max(counts.items(), key=lambda item: (int(item[1] or 0), item[0]))
    return label, int(count or 0)


def _rate(value: Any, denominator: int) -> float:
    if denominator <= 0:
        return 0.0
    try:
        return float(value or 0) / denominator * 100
    except (TypeError, ValueError):
        return 0.0


def _fmt_pct(value: Any) -> str:
    if not isinstance(value, (int, float)):
        return "-"
    sign = "+" if float(value) > 0 else ""
    return f"{sign}{float(value):.2f}%"


def _label_with_rate(group: Dict[str, Any]) -> str:
    label = str(group.get("dominant_label") or "unclassified")
    rate = group.get("dominant_label_rate_pct")
    return f"{label} ({_fmt_pct(rate)})" if isinstance(rate, (int, float)) else label


def _markdown_cell(value: Any) -> str:
    text = str(value if value is not None else "-").replace("\n", " ").strip()
    return text.replace("|", "\\|")


def _preferred_review_window(row: Dict[str, Any]) -> Dict[str, Any]:
    windows = row.get("windows") if isinstance(row.get("windows"), dict) else {}
    return windows.get("30") or windows.get("7") or _first_completed_window(windows)


def _nested_dict(payload: Dict[str, Any], *keys: str) -> Dict[str, Any]:
    current: Any = payload
    for key in keys:
        if not isinstance(current, dict):
            return {}
        current = current.get(key)
    return current if isinstance(current, dict) else {}


def _trace_id(trace_dir: Path, summary: Dict[str, Any]) -> str:
    artifact_dir = str(summary.get("artifact_dir") or "")
    if artifact_dir:
        return Path(artifact_dir).name
    return trace_dir.name


def _is_stock_selection_report(final_report: Dict[str, Any]) -> bool:
    return bool(
        isinstance(final_report.get("judge_decision"), dict)
        and isinstance(final_report.get("portfolio_allocation"), dict)
        and (
            isinstance(final_report.get("candidate_discovery"), dict)
            or isinstance(final_report.get("single_stock_deep_dive"), dict)
        )
    )


def _resolve_single_stock_symbol(
    *,
    final_report: Dict[str, Any],
    request: Dict[str, Any],
    context: Dict[str, Any],
    risk_gate: Dict[str, Any],
) -> str:
    trade_plan = risk_gate.get("trade_plan") if isinstance(risk_gate.get("trade_plan"), dict) else {}
    context_payload = context.get("context") if isinstance(context.get("context"), dict) else {}
    context_summary = context.get("context_summary") if isinstance(context.get("context_summary"), dict) else {}
    for value in (
        trade_plan.get("symbol"),
        request.get("stock_code"),
        context_payload.get("stock_code"),
        context_summary.get("stock_code"),
        final_report.get("stock_code"),
        final_report.get("code"),
    ):
        normalized = _normalize_symbol(value)
        if normalized:
            return normalized
    return ""


def _resolve_single_stock_name(final_report: Dict[str, Any], request: Dict[str, Any], context: Dict[str, Any]) -> Optional[str]:
    context_payload = context.get("context") if isinstance(context.get("context"), dict) else {}
    for value in (
        final_report.get("stock_name"),
        request.get("stock_name"),
        context_payload.get("stock_name"),
    ):
        text = str(value or "").strip()
        if text:
            return text
    return None


def _resolve_single_stock_intent(request: Dict[str, Any], context: Dict[str, Any]) -> Optional[str]:
    if request.get("report_intent"):
        return str(request.get("report_intent"))
    context_payload = context.get("context") if isinstance(context.get("context"), dict) else {}
    agent_context = context_payload.get("agent_user_context") if isinstance(context_payload.get("agent_user_context"), dict) else {}
    report = agent_context.get("report") if isinstance(agent_context.get("report"), dict) else {}
    if report.get("intent"):
        return str(report.get("intent"))
    return "single_stock_analysis"


def _resolve_single_stock_decision_date(final_report: Dict[str, Any], trace_dir: Path) -> date:
    for value in (
        final_report.get("analysis_date"),
        final_report.get("date"),
        _date_from_trace_dir(trace_dir),
    ):
        parsed = _parse_date(value)
        if parsed is not None:
            return parsed
    return date.today()


def _resolve_single_stock_action(final_report: Dict[str, Any], risk_gate: Dict[str, Any]) -> str:
    gate = risk_gate.get("risk_gate") if isinstance(risk_gate.get("risk_gate"), dict) else {}
    trade_plan = risk_gate.get("trade_plan") if isinstance(risk_gate.get("trade_plan"), dict) else {}
    for value in (
        gate.get("allowed_action"),
        trade_plan.get("action"),
        final_report.get("operation_advice"),
        final_report.get("decision_type"),
    ):
        action = _normalize_single_stock_action(value)
        if action:
            return action
    return "wait"


def _normalize_single_stock_action(value: Any) -> str:
    text = str(value or "").strip().lower()
    if not text:
        return ""
    if text in {"buy", "open", "add", "买入", "加仓", "增持", "强烈买入"}:
        return "open" if text in {"buy", "买入", "强烈买入"} else "add"
    if text in {"sell", "reduce", "trim", "卖出", "减仓", "清仓", "强烈卖出"}:
        return "sell" if text in {"sell", "卖出", "清仓", "强烈卖出"} else "reduce"
    if text in {"hold", "monitor", "wait", "manual_review", "持有", "观望", "等待", "震荡"}:
        return "hold" if text in {"hold", "持有"} else "wait"
    return ""


def _resolve_single_stock_confidence(final_report: Dict[str, Any]) -> Optional[float]:
    value = final_report.get("confidence")
    if value is None:
        value = final_report.get("confidence_score")
    if value is None:
        label = str(final_report.get("confidence_level") or "").strip()
        return {"高": 0.8, "中": 0.55, "低": 0.3, "high": 0.8, "medium": 0.55, "low": 0.3}.get(label.lower() if label.isascii() else label)
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return None
    return numeric / 100.0 if numeric > 1 else numeric


def _resolve_decision_date(final_report: Dict[str, Any], trace_dir: Path) -> date:
    market_regime = final_report.get("market_regime") if isinstance(final_report.get("market_regime"), dict) else {}
    for value in (
        market_regime.get("as_of"),
        market_regime.get("date"),
        _date_from_trace_dir(trace_dir),
    ):
        parsed = _parse_date(value)
        if parsed is not None:
            return parsed
    return date.today()


def _date_from_trace_dir(trace_dir: Path) -> Optional[str]:
    match = re.match(r"(\d{8})-", trace_dir.name)
    if not match:
        return None
    value = match.group(1)
    return f"{value[:4]}-{value[4:6]}-{value[6:8]}"


def _parse_date(value: Any) -> Optional[date]:
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    if not value:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def _extract_review_symbols(final_report: Dict[str, Any]) -> List[Dict[str, Any]]:
    positions = _nested_dict(final_report, "portfolio_allocation", "full").get("positions_plan")
    items: List[Dict[str, Any]] = []
    if isinstance(positions, list):
        items.extend(item for item in positions if isinstance(item, dict) and item.get("code"))
    if not items:
        candidates = _nested_dict(final_report, "candidate_discovery", "full").get("candidates")
        if isinstance(candidates, list):
            items.extend(item for item in candidates if isinstance(item, dict) and item.get("code"))
    if not items:
        codes = _nested_dict(final_report, "candidate_discovery", "summary").get("candidate_codes")
        if isinstance(codes, list):
            items.extend({"code": code} for code in codes if code)
    deduped: Dict[str, Dict[str, Any]] = {}
    for item in items:
        code = str(item.get("code") or "").strip()
        if code and code not in deduped:
            deduped[code] = {"code": code, "name": item.get("name")}
    return list(deduped.values())


def _plan_for_symbol(positions: Any, symbol: str) -> Dict[str, Any]:
    if not isinstance(positions, list):
        return {}
    normalized = _normalize_symbol(symbol)
    for item in positions:
        if isinstance(item, dict) and _normalize_symbol(item.get("code")) == normalized:
            return item
    return {}


def _resolve_symbol_action(plan: Dict[str, Any], judge_summary: Dict[str, Any]) -> str:
    for value in (plan.get("action"), plan.get("execution_mode"), judge_summary.get("final_action")):
        if value:
            return str(value).strip().lower()
    return "wait"


def _resolve_confidence(judge_summary: Dict[str, Any], plan: Dict[str, Any]) -> Optional[float]:
    for value in (judge_summary.get("confidence"), plan.get("confidence"), plan.get("action_strength_score")):
        try:
            if value is not None and value != "":
                return float(value)
        except (TypeError, ValueError):
            continue
    return None


def _resolve_intent(request: Dict[str, Any], final_report: Dict[str, Any]) -> Optional[str]:
    if request.get("report_intent"):
        return str(request.get("report_intent"))
    selection_context = final_report.get("selection_context") if isinstance(final_report.get("selection_context"), dict) else {}
    message = str(selection_context.get("user_message") or request.get("message") or "").strip()
    if "选股" in message or "入手" in message:
        return "watchlist_scan"
    return None


def _resolve_data_quality(market_regime: Dict[str, Any], evaluation: Dict[str, Any]) -> str:
    if evaluation.get("status") == "insufficient_start_price":
        return "insufficient_price"
    if evaluation.get("limits"):
        return "partial"
    value = market_regime.get("data_quality")
    if isinstance(value, dict):
        status = value.get("status")
        if status:
            return str(status)
    return "ok"


def _operation_advice_for_action(action: str) -> str:
    normalized = str(action or "").lower()
    if normalized in {"open", "buy", "add", "conditional_open", "immediate_open"}:
        return "买入"
    if normalized in {"hold", "monitor", "strong_watch"}:
        return "持有"
    if normalized in {"trim", "reduce", "sell"}:
        return "减仓"
    return "等待"


def _classify_review_label(*, action: str, evaluation: Dict[str, Any]) -> str:
    windows = evaluation.get("windows") if isinstance(evaluation.get("windows"), dict) else {}
    preferred = windows.get("30") or windows.get("7") or _first_completed_window(windows)
    if not isinstance(preferred, dict) or preferred.get("eval_status") != "completed":
        return "insufficient_data"
    future_return = preferred.get("future_return_pct")
    if future_return is None:
        return "insufficient_data"
    normalized = str(action or "").lower()
    active = normalized in {"open", "buy", "add", "conditional_open", "immediate_open", "hold"}
    defensive = normalized in {"wait", "reject", "monitor", "plain_wait", "strong_watch", "trim", "reduce", "sell"}
    if active:
        if future_return > 2:
            return "hit"
        if future_return < -2:
            return "wrong_direction"
        return "no_edge"
    if defensive:
        if future_return > 2:
            return "missed_up"
        if future_return < -2:
            return "avoided_down"
        return "neutral_ok"
    return "unclassified"


def _first_completed_window(windows: Dict[str, Any]) -> Dict[str, Any]:
    for item in windows.values():
        if isinstance(item, dict) and item.get("eval_status") == "completed":
            return item
    return {}


def _stock_code_candidates(symbol: str) -> List[str]:
    text = str(symbol or "").strip()
    if not text:
        return []
    candidates = [text]
    if "." in text:
        candidates.append(text.split(".", 1)[0])
    upper = text.upper()
    if upper.endswith((".SH", ".SZ", ".BJ")):
        candidates.append(upper[:-3])
    return list(dict.fromkeys(candidates))


def _normalize_symbol(value: Any) -> str:
    text = str(value or "").strip().upper()
    return text.split(".", 1)[0] if "." in text else text


def _normalize_windows(eval_windows: Sequence[int]) -> List[int]:
    windows: List[int] = []
    for item in eval_windows:
        try:
            value = int(item)
        except (TypeError, ValueError):
            continue
        if value > 0 and value not in windows:
            windows.append(value)
    return windows or list(DEFAULT_EVAL_WINDOWS)


def _round_or_none(value: Any, digits: int = 4) -> Optional[float]:
    try:
        if value is None:
            return None
        return round(float(value), digits)
    except (TypeError, ValueError):
        return None


__all__ = ["AgentVerdictReviewService", "VerdictReviewBuildResult"]
