# -*- coding: utf-8 -*-
"""
Agent API endpoints.
"""

import asyncio
import json
import logging
import math
import os
import re
import uuid
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional, TypedDict

from fastapi import APIRouter, BackgroundTasks, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import AliasChoices, BaseModel, Field

from src.config import get_config
from src.agent.candidate_experts_v2.seed_facts import compact_seed_fact_packets_for_model
from src.agent.llm_telemetry import llm_telemetry_scope
from src.agent.scoring import CompositeSnapshotBuilder, evaluate_composite_verdict, score_symbol_composite
from src.schemas.agent_context import AgentUserContext, ReportContext, ReportIntent
from src.schemas.agent_signal import TradeAction, TradePlan
from src.schemas.composite_verdict import CompositeScoreSnapshot
from src.services.agent_model_service import list_agent_model_deployments

try:
    from src.services.graphiti import get_graphiti_service
except Exception:  # pragma: no cover
    get_graphiti_service = None

# Tool name -> Chinese display name mapping
TOOL_DISPLAY_NAMES: Dict[str, str] = {
    "discover_watchlist_candidates": "发现候选股",
    "get_realtime_quote":         "获取实时行情",
    "get_daily_history":          "获取历史K线",
    "get_chip_distribution":      "分析筹码分布",
    "get_analysis_context":       "获取分析上下文",
    "get_stock_info":             "旧版基本面聚合（已废弃）",
    "search_stock_news":          "搜索股票新闻",
    "search_comprehensive_intel": "搜索综合情报",
    "search_stock_fundamentals":  "补充基本面公开资料",
    "get_tushare_today_news":     "获取当日新闻快讯",
    "analyze_trend":              "分析技术趋势",
    "calculate_ma":               "计算均线系统",
    "get_volume_analysis":        "分析量能变化",
    "analyze_pattern":            "识别K线形态",
    "get_market_indices":         "获取市场指数",
    "get_sector_rankings":        "分析行业板块",
    "get_skill_backtest_summary": "获取技能回测概览",
    "get_strategy_backtest_summary": "获取策略回测概览",
    "get_stock_backtest_summary": "获取个股回测数据",
}

logger = logging.getLogger(__name__)

router = APIRouter()


class TraceIntentResolution(TypedDict, total=False):
    source: Literal["mimo", "explicit", "default"]
    intent: ReportIntent
    requested_intent: Optional[str]
    stock_code_present: bool
    classifier_configured: bool
    classifier_model: str
    classifier_success: bool
    classifier_intent: Optional[ReportIntent]
    classifier_error: Optional[str]

class AgentTraceRunRequest(BaseModel):
    message: str = Field(..., min_length=1, max_length=20000)
    session_id: Optional[str] = None
    account_id: Optional[int] = None
    stock_code: Optional[str] = None
    stock_name: Optional[str] = None
    skills: Optional[List[str]] = Field(default=None, validation_alias=AliasChoices("skills", "strategies"))
    context: Optional[Dict[str, Any]] = None
    inject_portfolio_context: bool = True
    analysis_mode: str = "planning_execute"
    report_intent: Optional[ReportIntent] = None
    risk_preference: Optional[str] = None
    trading_horizon: Optional[str] = None
    max_single_position_pct: Optional[float] = Field(default=None, ge=0, le=100)
    max_total_equity_exposure_pct: Optional[float] = Field(default=None, ge=0, le=100)
    max_acceptable_drawdown_pct: Optional[float] = Field(default=None, ge=0, le=100)
    default_stop_loss_pct: Optional[float] = Field(default=None, ge=0, le=100)
    investor_notes: Optional[str] = None
    candidate_discovery_mode: Optional[Literal["deterministic", "llm_expert_committee", "thesis_desk_committee"]] = None
    resume_from_session_id: Optional[str] = None

    @property
    def effective_skills(self) -> Optional[List[str]]:
        return self.skills


class AgentTraceRunResponse(BaseModel):
    success: bool
    session_id: str
    content: str = ""
    error: Optional[str] = None
    total_steps: int = 0
    total_tokens: int = 0
    provider: str = ""
    model: str = ""
    mode: str = ""
    events: List[Dict[str, Any]] = Field(default_factory=list)
    tool_calls: List[Dict[str, Any]] = Field(default_factory=list)
    planner: Optional[Dict[str, Any]] = None
    agent_user_context: Optional[Dict[str, Any]] = None
    context_summary: Optional[Dict[str, Any]] = None
    dimension_evidence: Optional[Dict[str, Any]] = None
    report_quality: Optional[Dict[str, Any]] = None
    debate: Optional[Dict[str, Any]] = None
    stock_selection: Optional[Dict[str, Any]] = None
    composite_verdict: Optional[Dict[str, Any]] = None
    risk_gate: Optional[Dict[str, Any]] = None
    price_plan: Optional[Dict[str, Any]] = None
    trade_plan_result: Optional[Dict[str, Any]] = None
    llm_telemetry: Optional[Dict[str, Any]] = None
    judge_sanity: Optional[Dict[str, Any]] = None
    artifact_dir: Optional[str] = None
    runtime_config: Optional[Dict[str, Any]] = None

class AgentModelDeployment(BaseModel):
    deployment_id: str
    model: str
    provider: str
    source: str
    api_base: Optional[str] = None
    deployment_name: Optional[str] = None
    is_primary: bool = False
    is_fallback: bool = False


class AgentModelsResponse(BaseModel):
    models: List[AgentModelDeployment]


class AgentRuntimeConfigResponse(BaseModel):
    runtime_config: Dict[str, Any]


@router.get("/models", response_model=AgentModelsResponse)
async def get_agent_models():
    """Get configured Agent model deployments for frontend selection."""
    config = get_config()
    return AgentModelsResponse(
        models=[AgentModelDeployment(**item) for item in list_agent_model_deployments(config)]
    )


@router.get("/runtime-config", response_model=AgentRuntimeConfigResponse)
async def get_agent_runtime_config():
    """Return non-sensitive Agent runtime switches for frontend diagnostics."""
    return AgentRuntimeConfigResponse(runtime_config=_build_agent_runtime_config(get_config()))


def _build_executor(config, skills: Optional[List[str]] = None):
    """Build and return a configured AgentExecutor (sync helper)."""
    from src.agent.factory import build_agent_executor
    return build_agent_executor(config, skills=skills)


def _trace_message_symbols(message: str) -> List[str]:
    """Extract explicit securities, excluding common analysis abbreviations."""
    import re
    from data_provider.base import canonical_stock_code, normalize_stock_code

    pattern = (
        r"(?<![A-Za-z0-9])(?:"
        r"(?i:(?:SH|SZ|BJ)?\d{6}(?:\.(?:SH|SZ|BJ|SS))?|HK\s?\d{1,5}|\d{1,5}\.HK)"
        r"|[A-Z]{1,5}(?:\.[A-Z])?)(?![A-Za-z0-9])"
    )
    reserved = {"AGENT", "PLAN", "API", "LLM", "AI", "ETF", "MACD", "KDJ", "RSI", "ATR",
                "MA", "EMA", "SMA", "BOLL", "SMC", "BOS", "OHLCV", "PE", "PB", "ROE", "EPS",
                "USD", "CNY", "HKD", "T", "N", "A"}
    return list(dict.fromkeys(
        canonical_stock_code(normalize_stock_code(match.group(0).replace(" ", "")))
        for match in re.finditer(pattern, message or "")
        if match.group(0) not in reserved
    ))


def _trace_target_conflict(message: str, stock_code: Any) -> Optional[Dict[str, Any]]:
    from data_provider.base import canonical_stock_code, normalize_stock_code

    symbols = _trace_message_symbols(message)
    supplied = canonical_stock_code(normalize_stock_code(str(stock_code or "").replace(" ", "")))
    if len(symbols) != 1 or not supplied or supplied == symbols[0]:
        return None
    return {
        "error": "trace_target_conflict", "expected_symbol": symbols[0], "actual_symbol": supplied,
        "message": f"问题中的股票 {symbols[0]} 与配置股票 {supplied} 不一致，请清空或修改股票代码后重新运行。",
    }


def _stored_trace_target_conflict(path: Path) -> Optional[Dict[str, Any]]:
    request = _read_trace_json_file(path / "request.json", {}) or {}
    context = _read_trace_json_file(path / "context.json", {}) or {}
    user = context.get("agent_user_context") or {}
    report = user.get("report") or {}
    message = str(request.get("message") or report.get("user_prompt") or "")
    for symbol in (request.get("stock_code"), report.get("primary_symbol")):
        conflict = _trace_target_conflict(message, symbol)
        if conflict:
            return conflict
    return None


def _infer_stock_code_from_message(message: str) -> str:
    """Best-effort stock code extraction for developer trace convenience."""
    import re

    symbols = _trace_message_symbols(message)
    if len(symbols) == 1:
        return symbols[0]
    patterns = [
        r"(?<!\d)(?:SH|SZ|BJ)?(\d{6})(?:\.(?:SH|SZ|BJ|SS))?(?!\d)",
        r"\bHK\s?(\d{1,5})\b",
        r"\b(\d{5})\.HK\b",
        r"\b([A-Z]{1,5}(?:\.[A-Z])?)\b",
    ]
    for pattern in patterns:
        match = re.search(pattern, message.strip(), flags=re.IGNORECASE)
        if not match:
            continue
        value = match.group(0).strip().upper().replace(" ", "")
        if value in {"AGENT", "PLAN", "EXECUTE", "API", "LLM"}:
            continue
        if value.endswith(".HK") and value[:5].isdigit():
            return "HK" + value[:5]
        return value
    return ""


def _build_trace_context(
    *,
    request: AgentTraceRunRequest,
    config: Any,
) -> Dict[str, Any]:
    context = dict(request.context or {})
    for symbol in (request.stock_code, context.get("stock_code")):
        conflict = _trace_target_conflict(request.message, symbol)
        if conflict:
            raise HTTPException(status_code=422, detail=conflict)
    resume_session_id = _normalize_trace_session_id(request.resume_from_session_id)
    if resume_session_id:
        resume_artifact_dir = _find_trace_artifact_dir(resume_session_id)
        if resume_artifact_dir is None:
            raise HTTPException(
                status_code=404,
                detail={"error": "resume_trace_not_found", "message": f"未找到可继续运行的 Trace {resume_session_id}"},
            )
        if _stored_trace_target_conflict(resume_artifact_dir):
            raise HTTPException(status_code=409, detail={
                "error": "resume_trace_target_conflict",
                "message": "原 Trace 存在股票目标冲突，不能复用其证据；请重新运行分析。",
            })
        context["resume_from_session_id"] = resume_session_id
        context["stock_selection_resume_artifact_dir"] = str(resume_artifact_dir)
        context["planning_ledger_reuse"] = _build_planning_ledger_reuse_summary(
            resume_artifact_dir,
            source_trace_id=resume_session_id,
        )
    explicit_stock_code = (
        request.stock_code
        or context.get("stock_code")
        or _infer_stock_code_from_message(request.message)
        or ""
    ).strip()
    stock_code = explicit_stock_code
    target_resolution: Dict[str, Any] = {
        "status": (
            "resolved"
            if stock_code
            else "unresolved"
            if request.stock_name
            else "not_requested"
        ),
        "source": "explicit_code" if stock_code else "none",
        "symbol": stock_code or None,
        "requested_stock_name": request.stock_name,
    }
    if not stock_code:
        try:
            from src.services.name_to_code_resolver import resolve_name_to_code

            stock_reference = str(request.stock_name or request.message or "").strip()
            stock_code = str(
                resolve_name_to_code(stock_reference, allow_network=False) or ""
            ).strip()
            if stock_code:
                target_resolution.update({
                    "status": "resolved",
                    "source": "local_name_index",
                    "symbol": stock_code,
                })
        except Exception as exc:
            target_resolution.update({
                "status": "failed",
                "source": "local_name_index",
                "error": str(exc),
            })
            logger.warning("Agent trace stock target resolution failed: %s", exc)
    context["_trace_target_resolution"] = target_resolution
    if stock_code:
        context["stock_code"] = stock_code
    if request.stock_name:
        context["stock_name"] = request.stock_name
    context.setdefault("report_type", "detailed")
    context.setdefault("report_language", getattr(config, "report_language", "zh"))
    intent_resolution = _resolve_trace_report_intent(request=request, stock_code=stock_code)
    context["_trace_intent_resolution"] = intent_resolution
    if intent_resolution["intent"] == "watchlist_scan" and not stock_code:
        target_resolution["status"] = "not_applicable"

    should_inject = (
        (request.inject_portfolio_context or request.account_id is not None)
        and request.analysis_mode == "planning_execute"
    )
    if should_inject and "agent_user_context" not in context:
        try:
            from src.agent.context_builder import build_agent_user_context_from_portfolio_service
            from src.services.portfolio_service import PortfolioService

            context["agent_user_context"] = build_agent_user_context_from_portfolio_service(
                PortfolioService(),
                account_id=request.account_id,
                symbol=stock_code or None,
                cost_method="fifo",
                user_prompt=request.message,
                analysis_mode="planning_execute",
                report_language=context.get("report_language", "zh"),
            )
            _apply_trace_investor_profile(
                context["agent_user_context"],
                risk_preference=request.risk_preference,
                trading_horizon=request.trading_horizon,
                max_single_position_pct=request.max_single_position_pct,
                max_total_equity_exposure_pct=request.max_total_equity_exposure_pct,
                max_acceptable_drawdown_pct=request.max_acceptable_drawdown_pct,
                default_stop_loss_pct=request.default_stop_loss_pct,
                notes=request.investor_notes,
            )
            _normalize_injected_report_intent(
                context["agent_user_context"],
                stock_code=stock_code,
                user_message=request.message,
            )
            _apply_trace_report_overrides(
                context["agent_user_context"],
                report_intent=(
                    intent_resolution["intent"]
                    if _should_override_injected_report_intent(intent_resolution)
                    else None
                ),
            )
        except Exception as exc:
            logger.warning("Agent trace portfolio context injection failed: %s", exc)
            context["_trace_context_error"] = str(exc)

    if request.analysis_mode == "planning_execute" and "agent_user_context" not in context:
        context["agent_user_context"] = _build_minimal_trace_agent_user_context(
            request=request,
            stock_code=stock_code,
            language=context.get("report_language", "zh"),
            intent_resolution=intent_resolution,
        )

    position_name_symbols = _trace_position_name_symbols(
        context.get("agent_user_context"),
        request.message,
    )
    if len(position_name_symbols) > 1 and not explicit_stock_code:
        stock_code = ""
        context.pop("stock_code", None)
        target_resolution.update({
            "status": "ambiguous",
            "source": "portfolio_position_names",
            "symbol": None,
            "candidate_symbols": position_name_symbols,
            "error": "multiple_stock_names_in_request",
        })
    elif not stock_code and len(position_name_symbols) == 1:
        stock_code = position_name_symbols[0]
        context["stock_code"] = stock_code
        target_resolution.update({
            "status": "resolved",
            "source": "portfolio_position_name",
            "symbol": stock_code,
            "candidate_symbols": position_name_symbols,
        })

    if stock_code:
        _apply_trace_target_symbol(context.get("agent_user_context"), stock_code)
        intent_resolution["stock_code_present"] = True
    elif target_resolution.get("status") == "not_requested":
        target_resolution["status"] = "not_applicable"

    if request.candidate_discovery_mode is not None:
        context["candidate_discovery_mode"] = request.candidate_discovery_mode

    return context


def _trace_position_name_symbols(agent_user_context: Any, message: str) -> List[str]:
    """Resolve position symbols whose stored names occur in the request text."""
    if not agent_user_context or not message:
        return []
    positions = getattr(agent_user_context, "positions", None)
    if positions is None and isinstance(agent_user_context, dict):
        positions = agent_user_context.get("positions")
    candidates: List[Any] = []
    for position in positions or []:
        if isinstance(position, dict):
            symbol = position.get("symbol")
            stock_name = position.get("stock_name") or position.get("name")
        else:
            symbol = getattr(position, "symbol", None)
            stock_name = getattr(position, "stock_name", None) or getattr(position, "name", None)
        name = str(stock_name or "").strip()
        if name and name in message:
            candidates.append(symbol)
    return _trace_unique_symbols(candidates)


def _apply_trace_target_symbol(agent_user_context: Any, stock_code: str) -> None:
    """Keep the resolved single-stock target synchronized across context layers."""
    if not agent_user_context or not stock_code:
        return
    report = getattr(agent_user_context, "report", None)
    if report is None and isinstance(agent_user_context, dict):
        report = agent_user_context.setdefault("report", {})
    if report is None:
        return
    if isinstance(report, dict):
        targets = [str(item) for item in report.get("target_symbols") or [] if item]
        report["primary_symbol"] = stock_code
        report["target_symbols"] = [stock_code, *(item for item in targets if item != stock_code)]
        return
    targets = [str(item) for item in getattr(report, "target_symbols", []) or [] if item]
    report.primary_symbol = stock_code
    report.target_symbols = [stock_code, *(item for item in targets if item != stock_code)]


def _trace_composite_symbol(context: Dict[str, Any]) -> str:
    symbol = str(context.get("stock_code") or "").strip()
    agent_user_context = _coerce_trace_agent_user_context(context)
    report = getattr(agent_user_context, "report", None)
    intent = str(getattr(report, "intent", "") or "")
    if not symbol or intent == "watchlist_scan":
        return ""
    return symbol


def _prefetch_trace_composite_verdict(context: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Build single-stock scoring evidence before the LLM starts reasoning."""
    symbol = _trace_composite_symbol(context)
    if not symbol:
        target_resolution = context.get("_trace_target_resolution")
        if isinstance(target_resolution, dict) and target_resolution.get("status") in {"failed", "ambiguous", "unresolved"}:
            return {
                "schema_version": "stock-composite-shadow.v1",
                "status": "failed",
                "error_code": "target_unresolved",
                "error": "无法唯一解析单股目标，策略 4 与缠论评分未执行。",
                "target_resolution": target_resolution,
            }
        return None
    try:
        from src.services.history_loader import get_frozen_target_date

        agent_user_context = _coerce_trace_agent_user_context(context)
        if agent_user_context is None:
            return None
        as_of = context.get("as_of") or get_frozen_target_date() or datetime.now().date()
        return score_symbol_composite(
            symbol,
            as_of=as_of,
            quote_basis="latest_trading_day",
            holding=_find_trace_position(agent_user_context, symbol) is not None,
            snapshot_builder=CompositeSnapshotBuilder(fallback_to_network=True),
        )
    except Exception as exc:
        logger.warning("Agent trace composite prefetch failed for %s: %s", symbol, exc)
        return {
            "schema_version": "stock-composite-shadow.v1",
            "status": "failed",
            "symbol": symbol,
            "error": str(exc),
        }


def _normalize_injected_report_intent(agent_user_context: Any, *, stock_code: str, user_message: str = "") -> None:
    report = getattr(agent_user_context, "report", None)
    if report is None:
        return
    current_intent = getattr(report, "intent", None)
    if isinstance(report, dict):
        current_intent = report.get("intent")
    if current_intent not in {None, "", "auto"}:
        return
    target_symbols = list(getattr(report, "target_symbols", []) or [])
    if isinstance(report, dict):
        target_symbols = list(report.get("target_symbols") or [])
    primary_symbol = getattr(report, "primary_symbol", None)
    if isinstance(report, dict):
        primary_symbol = report.get("primary_symbol")
    target = str(stock_code or primary_symbol or (target_symbols[0] if target_symbols else "") or "").strip()
    if stock_code:
        target = stock_code
        if stock_code not in target_symbols:
            target_symbols.insert(0, stock_code)
        else:
            target_symbols = [stock_code] + [symbol for symbol in target_symbols if symbol != stock_code]
        if isinstance(report, dict):
            report["primary_symbol"] = stock_code
            report["target_symbols"] = target_symbols
        else:
            try:
                setattr(report, "primary_symbol", stock_code)
                setattr(report, "target_symbols", target_symbols)
            except Exception:
                logger.debug("Agent trace report target normalization skipped")
    positions = getattr(agent_user_context, "positions", []) or []
    has_position = bool(target and hasattr(agent_user_context, "has_position_for") and agent_user_context.has_position_for(target))
    has_any_position = any(getattr(position, "quantity", 0) for position in positions)
    intent = "position_review" if (has_position or (has_any_position and not target)) else (
        "entry_analysis" if target else ("watchlist_scan" if _message_explicitly_requests_watchlist_scan(user_message) else "qa")
    )
    if isinstance(report, dict):
        report["intent"] = intent
        report["include_watchlist_ranking"] = intent == "watchlist_scan"
        return
    try:
        setattr(report, "intent", intent)
        setattr(report, "include_watchlist_ranking", intent == "watchlist_scan")
    except Exception:
        logger.debug("Agent trace injected report intent normalization skipped")


def _should_override_injected_report_intent(intent_resolution: TraceIntentResolution) -> bool:
    """Only override portfolio-derived intent when the user or classifier said so.

    Portfolio context can already resolve a holding review from real positions.
    A classifier failure can still fall back to a deterministic explicit
    watchlist request; that user intent must override an injected
    ``position_review`` context, otherwise the staged stock-selection pipeline
    never starts.
    """
    return bool(
        intent_resolution.get("requested_intent")
        or (intent_resolution.get("source") == "mimo" and intent_resolution.get("classifier_success"))
        or (
            intent_resolution.get("intent") == "watchlist_scan"
            and intent_resolution.get("explicit_watchlist_request")
        )
    )


def _build_minimal_trace_agent_user_context(
    *,
    request: AgentTraceRunRequest,
    stock_code: str,
    language: str,
    intent_resolution: TraceIntentResolution,
) -> AgentUserContext:
    intent = intent_resolution["intent"]
    target_symbols = [stock_code] if stock_code else []
    context = AgentUserContext(
        report=ReportContext(
            intent=intent,
            analysis_mode="planning_execute",
            target_symbols=target_symbols,
            primary_symbol=stock_code or None,
            language="en" if language == "en" else "zh",
            include_entry_plan=True,
            include_position_plan=True,
            include_risk_review=True,
            include_watchlist_ranking=intent == "watchlist_scan",
            user_prompt=request.message,
        ),
    )
    _apply_trace_investor_profile(
        context,
        risk_preference=request.risk_preference,
        trading_horizon=request.trading_horizon,
        max_single_position_pct=request.max_single_position_pct,
        max_total_equity_exposure_pct=request.max_total_equity_exposure_pct,
        max_acceptable_drawdown_pct=request.max_acceptable_drawdown_pct,
        default_stop_loss_pct=request.default_stop_loss_pct,
        notes=request.investor_notes,
    )
    return context


def _resolve_trace_report_intent(*, request: AgentTraceRunRequest, stock_code: str) -> TraceIntentResolution:
    llm_intent, classifier_meta = _classify_trace_report_intent_with_mimo(request.message)
    requested_intent = request.report_intent if request.report_intent != "auto" else None
    explicit_watchlist_request = _message_explicitly_requests_watchlist_scan(request.message)
    base: TraceIntentResolution = {
        "requested_intent": requested_intent,
        "stock_code_present": bool(stock_code),
        "explicit_watchlist_request": explicit_watchlist_request,
        **classifier_meta,
    }
    if llm_intent:
        if llm_intent == "watchlist_scan":
            if explicit_watchlist_request or requested_intent == "watchlist_scan":
                return {**base, "source": "mimo", "intent": "watchlist_scan"}
            return {**base, "source": "mimo_guard", "intent": "qa"}
        if not requested_intent:
            return {**base, "source": "mimo", "intent": llm_intent}
    if requested_intent:
        return {**base, "source": "explicit", "intent": requested_intent}
    if stock_code:
        return {**base, "source": "default", "intent": "entry_analysis"}
    return {**base, "source": "default", "intent": "watchlist_scan" if explicit_watchlist_request else "qa"}


def _message_explicitly_requests_watchlist_scan(message: str) -> bool:
    text = str(message or "").strip().lower()
    if not text:
        return False
    positive_patterns = (
        "选股",
        "筛股",
        "候选股",
        "候选池",
        "推荐股票",
        "推荐几只",
        "推荐标的",
        "入手的股票",
        "可以买的股票",
        "可关注候选",
        "下周可以入手",
        "下周可入手",
        "下周买什么",
        "买什么股票",
        "配置股票",
        "股票池",
        "watchlist",
        "stock pick",
        "stock picks",
        "screen stocks",
        "pick stocks",
    )
    return any(pattern in text for pattern in positive_patterns)


def _classify_trace_report_intent_with_mimo(message: str) -> tuple[Optional[ReportIntent], Dict[str, Any]]:
    api_base = (os.getenv("XIAOMI_MIMO_URL") or "").strip()
    api_key = (os.getenv("XIAOMI_MIMO_KEY") or os.getenv("XIAOMI_MIMO_API_KEY") or "").strip()
    model_name = (os.getenv("XIAOMI_MIMO_MODEL") or "mimo-v2.5").strip()
    meta: Dict[str, Any] = {
        "classifier_configured": bool(api_base and api_key),
        "classifier_model": model_name,
        "classifier_success": False,
        "classifier_intent": None,
        "classifier_error": None,
    }
    if not api_base or not api_key:
        return None, meta
    try:
        import litellm
        from src.agent.runner import try_parse_json

        litellm_model = model_name if "/" in model_name else f"openai/{model_name}"
        response = litellm.completion(
            model=litellm_model,
            api_base=api_base.rstrip("/"),
            api_key=api_key,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "你是股票分析系统的意图分类器。只输出 JSON。"
                        "intent 必须是 watchlist_scan、entry_analysis、position_review、"
                        "event_impact、risk_review、qa 之一。"
                        "只有用户明确要求从市场中挑选/筛选/推荐/配置股票、构建候选池、"
                        "或询问下周/近期可以买什么股票时，才输出 watchlist_scan。"
                        "普通解释、复盘、账户风险、持仓诊断、单票分析、工具排障、文档和实现问题，"
                        "都不要输出 watchlist_scan，也不要触发候选池。"
                    ),
                },
                {
                    "role": "user",
                    "content": json.dumps({"message": message}, ensure_ascii=False),
                },
            ],
            temperature=0,
            max_tokens=600,
            timeout=20,
        )
        content = str(response.choices[0].message.content or "")
        parsed = try_parse_json(content) or {}
        intent = str(parsed.get("intent") or "").strip()
        if intent in {"watchlist_scan", "entry_analysis", "position_review", "event_impact", "risk_review", "qa"}:
            meta["classifier_success"] = True
            meta["classifier_intent"] = intent
            return intent, meta  # type: ignore[return-value]
        meta["classifier_error"] = "MiMo returned no parseable intent JSON"
    except Exception as exc:
        meta["classifier_error"] = str(exc)
        logger.warning("MiMo trace intent classification failed; falling back to explicit report intent/defaults: %s", exc)
    return None, meta


def _serialize_agent_user_context(value: Any) -> Optional[Dict[str, Any]]:
    if value is None:
        return None
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if isinstance(value, dict):
        return value
    return None


def _apply_trace_investor_profile(
    agent_user_context: Any,
    *,
    risk_preference: Optional[str],
    trading_horizon: Optional[str],
    max_single_position_pct: Optional[float],
    max_total_equity_exposure_pct: Optional[float],
    max_acceptable_drawdown_pct: Optional[float],
    default_stop_loss_pct: Optional[float],
    notes: Optional[str],
) -> None:
    investor = getattr(agent_user_context, "investor", None)
    if investor is None:
        if isinstance(agent_user_context, dict):
            investor = agent_user_context.setdefault("investor", {})
        else:
            return

    updates = {
        "risk_preference": risk_preference,
        "trading_horizon": trading_horizon,
        "max_single_position_pct": max_single_position_pct,
        "max_total_equity_exposure_pct": max_total_equity_exposure_pct,
        "max_acceptable_drawdown_pct": max_acceptable_drawdown_pct,
        "default_stop_loss_pct": default_stop_loss_pct,
        "notes": notes,
    }
    for key, value in updates.items():
        if value in (None, ""):
            continue
        if isinstance(investor, dict):
            investor[key] = value
        else:
            try:
                setattr(investor, key, value)
            except Exception:
                logger.debug("Agent trace investor profile field skipped: %s", key)


def _apply_trace_report_overrides(
    agent_user_context: Any,
    *,
    report_intent: Optional[str],
) -> None:
    if not report_intent:
        return
    report = getattr(agent_user_context, "report", None)
    if report is None:
        if isinstance(agent_user_context, dict):
            report = agent_user_context.setdefault("report", {})
        else:
            return
    if isinstance(report, dict):
        report["intent"] = report_intent
        report["include_watchlist_ranking"] = report_intent == "watchlist_scan"
        return
    try:
        setattr(report, "intent", report_intent)
        setattr(report, "include_watchlist_ranking", report_intent == "watchlist_scan")
    except Exception:
        logger.debug("Agent trace report intent override skipped")


def _build_trace_context_summary(context: Dict[str, Any]) -> Dict[str, Any]:
    payload = _serialize_agent_user_context(context.get("agent_user_context"))
    summary: Dict[str, Any] = {
        "context_error": context.get("_trace_context_error"),
        "intent_resolution": context.get("_trace_intent_resolution"),
        "target_resolution": context.get("_trace_target_resolution"),
        "stock_code": context.get("stock_code"),
        "candidate_discovery_mode": context.get("candidate_discovery_mode"),
        "account_count": 0,
        "position_count": 0,
        "target_position": None,
        "accounts": [],
        "investor": None,
        "metadata": {},
        "planning_ledger_reuse": context.get("planning_ledger_reuse"),
    }
    if not payload:
        return summary

    accounts = payload.get("accounts") or []
    positions = payload.get("positions") or []
    investor = payload.get("investor") or {}
    report = payload.get("report") or {}
    primary_symbol = report.get("primary_symbol") or context.get("stock_code")
    normalized_primary = str(primary_symbol or "").strip().lower()

    summary["account_count"] = len(accounts)
    summary["position_count"] = len(positions)
    summary["investor"] = {
        "risk_preference": investor.get("risk_preference"),
        "trading_horizon": investor.get("trading_horizon"),
        "max_single_position_pct": investor.get("max_single_position_pct"),
        "max_total_equity_exposure_pct": investor.get("max_total_equity_exposure_pct"),
        "max_acceptable_drawdown_pct": investor.get("max_acceptable_drawdown_pct"),
        "default_stop_loss_pct": investor.get("default_stop_loss_pct"),
        "notes": investor.get("notes"),
    }
    summary["accounts"] = [
        {
            "account_id": item.get("account_id"),
            "account_name": item.get("account_name"),
            "broker": item.get("broker"),
            "market": item.get("market"),
            "base_currency": item.get("base_currency"),
            "total_equity": item.get("total_equity"),
            "available_cash": item.get("available_cash"),
            "total_market_value": item.get("total_market_value"),
            "cost_method": item.get("cost_method"),
        }
        for item in accounts
        if isinstance(item, dict)
    ]
    summary["target_position"] = next(
        (
            {
                "symbol": item.get("symbol"),
                "account_id": item.get("account_id"),
                "quantity": item.get("quantity"),
                "avg_cost": item.get("avg_cost"),
                "last_price": item.get("last_price"),
                "market_value": item.get("market_value"),
                "unrealized_pnl": item.get("unrealized_pnl"),
                "unrealized_pnl_pct": item.get("unrealized_pnl_pct"),
                "position_pct": item.get("position_pct"),
            }
            for item in positions
            if isinstance(item, dict)
            and str(item.get("symbol") or "").strip().lower() == normalized_primary
        ),
        None,
    )
    summary["metadata"] = payload.get("metadata") or {}
    return summary


def _build_agent_runtime_config(config: Any) -> Dict[str, Any]:
    """Expose non-sensitive Agent runtime switches for Trace diagnostics."""
    return {
        "agent_mode": bool(getattr(config, "agent_mode", False)),
        "agent_analysis_mode": getattr(config, "agent_analysis_mode", None),
        "agent_orchestration_mode": getattr(config, "agent_orchestration_mode", None),
        "agent_candidate_discovery_mode": getattr(config, "agent_candidate_discovery_mode", None),
        "agent_arch": getattr(config, "agent_arch", None),
        "agent_max_steps": getattr(config, "agent_max_steps", None),
        "agent_orchestrator_timeout_s": getattr(config, "agent_orchestrator_timeout_s", None),
        "agent_tool_call_timeout_seconds": getattr(config, "agent_tool_call_timeout_seconds", None),
        "agent_candidate_expert_timeout_seconds": getattr(config, "agent_candidate_expert_timeout_seconds", None),
        "agent_candidate_min_avg_amount": getattr(config, "agent_candidate_min_avg_amount", None),
        "agent_candidate_min_listing_days": getattr(config, "agent_candidate_min_listing_days", None),
        "agent_candidate_blacklist_count": len(getattr(config, "agent_candidate_blacklist_codes", []) or []),
        "agent_candidate_enforce_name_code_match": getattr(config, "agent_candidate_enforce_name_code_match", None),
        "mimo_intent_classifier_configured": bool(
            (os.getenv("XIAOMI_MIMO_URL") or "").strip()
            and (os.getenv("XIAOMI_MIMO_KEY") or os.getenv("XIAOMI_MIMO_API_KEY") or "").strip()
        ),
        "mimo_intent_classifier_model": (os.getenv("XIAOMI_MIMO_MODEL") or "mimo-v2.5").strip(),
    }


def _build_planner_trace(context: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    agent_user_context = context.get("agent_user_context")
    if agent_user_context is None:
        return None
    try:
        from src.agent.executor import _coerce_agent_user_context
        from src.agent.factory import get_tool_registry
        from src.agent.planner import build_planning_result

        coerced = _coerce_agent_user_context(agent_user_context)
        if coerced is None:
            return None
        return build_planning_result(coerced, tool_registry=get_tool_registry()).to_dict()
    except Exception as exc:
        logger.warning("Agent trace planner build failed: %s", exc)
        return {"error": str(exc)}


def _trace_artifact_root() -> Path:
    return Path(get_config().database_path).expanduser().resolve().parent / "agent_traces"


def _safe_trace_session_dir_name(session_id: str) -> str:
    safe_session = "".join(ch if ch.isalnum() or ch in ("-", "_") else "-" for ch in session_id).strip("-")
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return f"{timestamp}-{safe_session or uuid.uuid4().hex}"


def _safe_trace_session_suffix(session_id: str) -> str:
    return "".join(ch if ch.isalnum() or ch in ("-", "_") else "-" for ch in session_id).strip("-")


def _read_trace_json_file(path: Path, default: Any = None) -> Any:
    try:
        if not path.exists():
            return default
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.debug("Trace artifact JSON read failed for %s: %s", path, exc)
        return default


def _read_trace_events(path: Path, *, limit: int = 500) -> List[Dict[str, Any]]:
    if not path.exists():
        return []
    events: List[Dict[str, Any]] = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(item, dict):
                events.append(item)
    except Exception as exc:
        logger.debug("Trace artifact event read failed for %s: %s", path, exc)
    return events[-limit:]


def _find_trace_artifact_dir(session_id: str) -> Optional[Path]:
    safe_session = _safe_trace_session_suffix(session_id)
    if not safe_session:
        return None
    root = _trace_artifact_root()
    if not root.exists():
        return None
    matches = [path for path in root.glob(f"*-{safe_session}") if path.is_dir()]
    if not matches:
        return None
    return max(matches, key=lambda path: path.stat().st_mtime)


def _build_planning_ledger_reuse_summary(artifact_dir: Path, *, source_trace_id: str) -> Dict[str, Any]:
    """Return a compact, prompt-safe summary of a previous trace todo.md."""
    todo_path = artifact_dir / "todo.md"
    text = _read_trace_text(todo_path)
    if not text:
        return {
            "schema_version": 1,
            "source_trace_id": source_trace_id,
            "artifact_dir": str(artifact_dir),
            "todo_path": str(todo_path),
            "reuse_status": "missing_todo",
            "exists": False,
            "open_items_count": 0,
            "completed_items_count": 0,
            "failed_or_degraded_count": 0,
            "has_planning_ledger_contract": False,
            "has_tool_handoff_contract": False,
            "reusable_sections": [],
            "reuse_rule": "do not reuse raw evidence; rebuild a fresh plan",
        }

    lines = [line.strip() for line in text.splitlines()]
    reusable_sections = [
        section
        for section in (
            "任务识别",
            "工具计划",
            "Replan 策略",
            "执行状态",
            "未调用计划工具",
        )
        if f"## {section}" in text
    ]
    failed_or_degraded_count = sum(
        1
        for line in lines
        if any(marker in line.lower() for marker in ("failed", "timeout", "degraded", "fallback", "unavailable"))
    )
    has_tool_handoff_contract = all(
        marker in text
        for marker in (
            "expected_result=",
            "downstream_use=",
            "fallback_on_failure=",
            "next_step=",
        )
    )
    has_planning_ledger_contract = "Planning Ledger 复用契约" in text or "reuse_payload=" in text
    return {
        "schema_version": 1,
        "source_trace_id": source_trace_id,
        "artifact_dir": str(artifact_dir),
        "todo_path": str(todo_path),
        "reuse_status": "eligible_as_prior" if has_tool_handoff_contract else "stale_contract",
        "exists": True,
        "open_items_count": sum(1 for line in lines if line.startswith("- [ ]")),
        "completed_items_count": sum(1 for line in lines if line.startswith("- [x]")),
        "failed_or_degraded_count": failed_or_degraded_count,
        "has_planning_ledger_contract": has_planning_ledger_contract,
        "has_tool_handoff_contract": has_tool_handoff_contract,
        "reusable_sections": reusable_sections,
        "reuse_rule": (
            "reuse only task identity, tool plan, open items, failed/degraded summaries, "
            "and execution status counts; revalidate realtime quote, capital flow, news, "
            "market regime, and account-sensitive facts before final action"
        ),
    }


def _read_trace_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except Exception:
        return ""


def _normalize_trace_session_id(session_id: Optional[str]) -> str:
    normalized = str(session_id or "").strip()
    if not normalized:
        return ""
    return normalized if normalized.startswith("trace-") else f"trace-{normalized}"


def _enrich_legacy_trace_provisional_score(payload: Any) -> Any:
    """Add read-only deterministic explanations missing from legacy artifacts."""
    if not isinstance(payload, dict) or payload.get("status") != "ok":
        return payload
    snapshot_payload = payload.get("snapshot")
    stored_verdict = payload.get("composite_verdict")
    if not isinstance(snapshot_payload, dict) or not isinstance(stored_verdict, dict):
        return payload
    s4_snapshot = snapshot_payload.get("s4")
    if not isinstance(s4_snapshot, dict):
        return payload

    try:
        snapshot = CompositeScoreSnapshot.model_validate(snapshot_payload)
        evaluated = evaluate_composite_verdict(snapshot)
    except Exception as exc:
        logger.warning("Legacy Trace provisional-score enrichment failed: %s", exc)
        return payload
    enriched_payload = dict(payload)
    enriched_verdict = dict(stored_verdict)
    stored_components = stored_verdict.get("components")
    enriched_components = dict(stored_components) if isinstance(stored_components, dict) else {}
    stored_s4 = enriched_components.get("s4")
    enriched_s4 = dict(stored_s4) if isinstance(stored_s4, dict) else {}
    evaluated_s4 = evaluated.components.s4

    enrichment_types: List[str] = []
    if (
        stored_verdict.get("score") is None
        and "provisional_score" not in stored_verdict
        and evaluated.provisional_score is not None
    ):
        enriched_s4["provisional_score"] = evaluated_s4.provisional_score
        enriched_s4["provisional_breakdown"] = dict(evaluated_s4.provisional_breakdown)
        enriched_verdict["provisional_score"] = evaluated.provisional_score
        enrichment_types.append("legacy_provisional_score")
    if "formalization_reason_codes" not in stored_verdict:
        enriched_verdict["formalization_reason_codes"] = list(
            evaluated.formalization_reason_codes
        )
        enrichment_types.append("formalization_reason_codes")
    if "quality_warning_codes" not in stored_verdict:
        enriched_verdict["quality_warning_codes"] = list(evaluated.quality_warning_codes)
        enrichment_types.append("quality_warning_codes")
    if not enrichment_types:
        return payload
    enriched_components["s4"] = enriched_s4
    enriched_verdict["components"] = enriched_components
    enriched_payload["composite_verdict"] = enriched_verdict
    enriched_payload["read_time_enrichment"] = {
        "type": enrichment_types[0],
        "types": enrichment_types,
        "rule_version": evaluated.rule_version,
        "persisted": False,
    }
    return enriched_payload


def _quarantine_legacy_debate_parse_failure(payload: Any) -> Any:
    """Correct legacy Debate artifacts that recorded parse failure as success."""
    if not isinstance(payload, dict) or payload.get("success") is not True:
        return payload
    debug_outputs = payload.get("debug_outputs")
    if not isinstance(debug_outputs, dict):
        return payload
    parse_error = next(
        (
            str(value).strip()
            for key, value in debug_outputs.items()
            if str(key).endswith("_parse_error") and str(value).strip()
        ),
        "",
    )
    if not parse_error:
        return payload
    quarantined = dict(payload)
    quarantined["success"] = False
    quarantined["error"] = parse_error
    quarantined["read_time_quarantine"] = {
        "type": "legacy_debate_parse_failure",
        "persisted": False,
    }
    return quarantined


def _refresh_trace_debate_presentation(content: str, debate: Any) -> str:
    """Re-render a successful stored Debate appendix without rewriting artifacts."""
    if not isinstance(debate, dict) or debate.get("success") is not True:
        return content
    from src.agent.debate import format_debate_appendix

    appendix = format_debate_appendix(debate).strip()
    if not appendix:
        return content
    marker = "## 对抗式辩论裁决"
    price_marker = "<!-- deterministic-price-plan:start -->"
    text = str(content or "").strip()
    start = text.find(marker)
    price_start = text.find(price_marker)
    if start >= 0:
        end = price_start if price_start > start else len(text)
        text = f"{text[:start].rstrip()}\n\n{appendix}{text[end:]}"
    elif price_start >= 0:
        text = f"{text[:price_start].rstrip()}\n\n{appendix}\n\n{text[price_start:]}"
    else:
        text = f"{text}\n\n{appendix}" if text else appendix
    return text.strip()


def _trace_history_item_from_artifact(session_id: str, path: Path) -> Dict[str, Any]:
    summary = _read_trace_json_file(path / "summary.json", {}) or {}
    request_payload = _read_trace_json_file(path / "request.json", {}) or {}
    context_payload = _read_trace_json_file(path / "context.json", {}) or {}
    target_conflict = _stored_trace_target_conflict(path)
    planner = _read_trace_json_file(path / "planner.json", None)
    tool_calls = _normalize_tool_calls_status(_read_trace_json_file(path / "tool_calls.json", []) or [])
    dimension_evidence = _read_trace_json_file(path / "dimensions.json", None)
    if not isinstance(dimension_evidence, dict):
        dimension_evidence = (summary.get("dimension_evidence") if isinstance(summary, dict) else None)
    if not isinstance(dimension_evidence, dict):
        from src.agent.dimension_evidence import build_dimension_evidence_summary

        dimension_evidence = build_dimension_evidence_summary(tool_calls)
    else:
        # Persisted multi-agent dimensions may contain opinions but predate the
        # shared tool-coverage schema. Keep both views without mutating files.
        from src.agent.dimension_evidence import build_dimension_evidence_summary

        computed_dimensions = build_dimension_evidence_summary(tool_calls)
        dimension_evidence = {
            dimension: (
                {
                    **stored,
                    "status": (
                        "degraded"
                        if computed_dimensions.get(dimension, {}).get("quality") == "unavailable"
                        else "completed"
                    ),
                    "quality": computed_dimensions.get(dimension),
                    "opinion": {
                        "quality": computed_dimensions.get(dimension, {}).get("quality"),
                        "evidence_tools": list(dict.fromkeys(
                            (computed_dimensions.get(dimension, {}).get("available_tools") or [])
                            + (computed_dimensions.get(dimension, {}).get("partial_tools") or [])
                        )),
                        "missing_evidence": computed_dimensions.get(dimension, {}).get("missing_evidence") or [],
                    },
                    "tool_evidence": computed_dimensions.get(dimension),
                }
                if isinstance(stored, dict) and stored.get("source") == "single_agent_tool_evidence"
                else {
                    **stored,
                    "tool_evidence": stored.get("tool_evidence") or computed_dimensions.get(dimension),
                }
                if isinstance(stored, dict)
                else computed_dimensions.get(dimension)
            )
            for dimension in set(computed_dimensions) | set(dimension_evidence)
            for stored in [dimension_evidence.get(dimension)]
        }
    final_content = ""
    try:
        final_content = (path / "final.md").read_text(encoding="utf-8")
    except Exception:
        final_content = ""
    if str(request_payload.get("analysis_mode") or "planning_execute") == "planning_execute":
        from src.agent.report_presentation import normalize_planning_execute_final_content

        final_content = normalize_planning_execute_final_content(final_content)
    stock_selection = _read_trace_json_file(path / "stock_selection.json", None)
    composite_verdict = _read_trace_json_file(
        path / "composite_verdict.json",
        summary.get("composite_verdict"),
    )
    risk_gate = _read_trace_json_file(path / "risk_gate.json", summary.get("risk_gate"))
    composite_verdict, risk_gate = _quarantine_stored_trace_target_mismatch(
        context=context_payload,
        tool_calls=tool_calls,
        composite_payload=composite_verdict,
        risk_gate_payload=risk_gate,
        expected_symbol=target_conflict["expected_symbol"] if target_conflict else None,
    )
    composite_verdict = _enrich_legacy_trace_provisional_score(composite_verdict)
    debate = _quarantine_legacy_debate_parse_failure(
        _read_trace_json_file(path / "debate.json", None)
    )
    report_quality = _read_trace_json_file(path / "report_quality.json", summary.get("report_quality"))
    if isinstance(report_quality, dict):
        from src.agent.report_presentation import planning_report_quality_issues

        request_language = str(request_payload.get("report_language") or "zh")
        consistency_issues = planning_report_quality_issues(
            final_content,
            report_language=request_language,
            tool_calls=tool_calls,
        )
        contradiction_issues = [
            issue for issue in consistency_issues
            if issue.startswith("report_contradicts_")
        ]
        if contradiction_issues:
            report_quality = dict(report_quality)
            report_quality["read_time_consistency"] = {
                "status": "inconsistent",
                "issues": contradiction_issues,
                "persisted": False,
                "message": "历史报告把已有工具证据误写为完全缺失，建议重跑该 Trace。",
            }
    if (
        report_quality is None
        and isinstance(debate, dict)
        and isinstance(debate.get("read_time_quarantine"), dict)
    ):
        report_quality = {
            "status": "legacy_incomplete",
            "source": "read_time_quarantine",
            "initial_issues": ["report_quality_not_recorded", "debate_json_parse_failed"],
            "final_issues": ["legacy_report_requires_rerun"],
            "repair_attempted": False,
            "persisted": False,
        }
        debug_outputs = debate.get("debug_outputs")
        if isinstance(debug_outputs, dict):
            primary_report_raw = str(debug_outputs.get("primary_report_raw") or "").strip()
            if primary_report_raw:
                final_content = primary_report_raw
    if str(request_payload.get("analysis_mode") or "planning_execute") == "planning_execute":
        from src.agent.report_presentation import normalize_planning_execute_final_content

        final_content = normalize_planning_execute_final_content(final_content)
    final_content = _refresh_trace_debate_presentation(final_content, debate)
    final_content = _refresh_trace_price_plan_presentation(
        final_content,
        risk_gate_payload=risk_gate,
        debate_payload=debate,
        tool_calls=tool_calls,
        summary=summary,
    )
    if target_conflict:
        mismatch_message = (
            f"本次问题指定 {target_conflict['expected_symbol']}，但分析上下文及价格计划使用了 "
            f"{target_conflict['actual_symbol']}。该报告存在标的混用，价格计划、裁决和复核结果已隔离；请重新运行分析。"
        )
        final_content = f"## 分析结果已隔离\n\n{mismatch_message}"
        report_quality = {"status": "target_mismatch", "persisted": False, **target_conflict}
        debate = {"enabled": True, "success": False, "error": "target_symbol_mismatch"}
    llm_telemetry = _read_trace_json_file(path / "llm_telemetry.json", None) or _build_llm_telemetry_summary(path)
    judge_sanity = _read_trace_json_file(path / "judge_sanity.json", None) or _build_judge_sanity_summary(stock_selection)
    events = _read_trace_events(path / "events.ndjson")
    agent_user_context = context_payload.get("agent_user_context") if isinstance(context_payload, dict) else None
    context_summary = (
        summary.get("context_summary")
        or (context_payload.get("context_summary") if isinstance(context_payload, dict) else None)
    )
    result = {
        "success": bool(summary.get("success")) and not target_conflict,
        "session_id": session_id,
        "content": final_content,
        "error": mismatch_message if target_conflict else summary.get("error"),
        "total_steps": int(summary.get("total_steps") or len(tool_calls)),
        "total_tokens": int(summary.get("total_tokens") or 0),
        "provider": str(summary.get("provider") or ""),
        "model": str(summary.get("model") or ""),
        "mode": str(request_payload.get("analysis_mode") or "planning_execute"),
        "events": events,
        "tool_calls": tool_calls,
        "planner": planner,
        "agent_user_context": agent_user_context,
        "context_summary": context_summary,
        "dimension_evidence": dimension_evidence,
        "report_quality": report_quality,
        "debate": debate,
        "stock_selection": stock_selection,
        "composite_verdict": composite_verdict,
        "risk_gate": risk_gate,
        "llm_telemetry": llm_telemetry,
        "judge_sanity": judge_sanity,
        "artifact_dir": str(path),
        "runtime_config": None,
    }
    message = str(request_payload.get("message") or "")
    stock_code = str(target_conflict["expected_symbol"] if target_conflict else request_payload.get("stock_code") or "")
    return {
        "id": session_id,
        "createdAt": datetime.fromtimestamp(path.stat().st_mtime).isoformat(),
        "message": message,
        "stockCode": stock_code,
        "accountId": request_payload.get("account_id"),
        "status": "success" if result["success"] else "error",
        "result": result,
    }


def _refresh_trace_price_plan_presentation(
    content: str,
    *,
    risk_gate_payload: Any,
    debate_payload: Any,
    tool_calls: List[Dict[str, Any]],
    summary: Dict[str, Any],
) -> str:
    """Render current read-model semantics without mutating immutable Trace files."""
    if not isinstance(risk_gate_payload, dict):
        return content
    raw_result = risk_gate_payload.get("trade_plan_result")
    if not isinstance(raw_result, dict):
        return content
    try:
        from types import SimpleNamespace

        from src.agent.pricing.decision import build_review_artifact
        from src.agent.pricing.report import merge_price_plan_into_report
        from src.agent.pricing.schemas import TradePlanResult

        stored = TradePlanResult.model_validate(raw_result)
        review_debate = debate_payload
        if (
            debate_payload is None
            and isinstance(summary.get("report_quality"), dict)
            and summary["report_quality"].get("status") == "fallback"
        ):
            review_debate = {
                "enabled": True,
                "success": False,
                "error": "legacy_report_fallback_skipped_debate",
            }
        review = build_review_artifact(
            stored.price_plan,
            result=SimpleNamespace(
                content=content,
                dashboard=None,
                trade_plan=None,
                debate=review_debate,
                provider=str(summary.get("provider") or ""),
                model=str(summary.get("model") or ""),
            ),
            tool_calls=tool_calls,
        )
        refreshed = stored.model_copy(update={"review": review or stored.review})
        return merge_price_plan_into_report(content, refreshed)
    except Exception as exc:
        logger.debug("Trace price-plan read model refresh failed: %s", exc)
        return content


@router.get("/trace/sessions/{session_id}")
async def get_agent_trace_session(session_id: str):
    """Load a completed Agent Trace from local artifacts by session id."""
    normalized = session_id.strip()
    if not normalized:
        raise HTTPException(status_code=400, detail={"error": "invalid_session_id"})
    if not normalized.startswith("trace-"):
        normalized = f"trace-{normalized}"
    artifact_dir = _find_trace_artifact_dir(normalized)
    if artifact_dir is None:
        raise HTTPException(
            status_code=404,
            detail={"error": "not_found", "message": f"未找到 Trace session {normalized} 的落盘记录"},
        )
    return _trace_history_item_from_artifact(normalized, artifact_dir)


def _write_trace_json(path: Path, payload: Any) -> None:
    sanitized = _sanitize_json_payload(payload)
    path.write_text(
        json.dumps(sanitized, ensure_ascii=False, indent=2, default=str, allow_nan=False),
        encoding="utf-8",
    )


def _write_trace_json_compact(path: Path, payload: Any) -> None:
    sanitized = _sanitize_json_payload(payload)
    path.write_text(
        json.dumps(
            sanitized,
            ensure_ascii=False,
            separators=(",", ":"),
            default=str,
            allow_nan=False,
        ) + "\n",
        encoding="utf-8",
    )


def _append_trace_event(path: Path, event: Dict[str, Any]) -> None:
    sanitized = _sanitize_json_payload(event)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(sanitized, ensure_ascii=False, default=str, allow_nan=False))
        fh.write("\n")


def _sanitize_json_payload(value: Any) -> Any:
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {key: _sanitize_json_payload(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_sanitize_json_payload(item) for item in value]
    if isinstance(value, tuple):
        return [_sanitize_json_payload(item) for item in value]
    return value


def _planner_to_todo_md(
    planner: Optional[Dict[str, Any]],
    context_summary: Optional[Dict[str, Any]],
    tool_calls: Optional[List[Dict[str, Any]]] = None,
) -> str:
    if not planner:
        lines = [
            "# todo",
            "",
            "## 计划状态",
            "- [ ] Planner 未生成",
        ]
        _append_execute_status_to_todo(lines, [], tool_calls or [])
        return "\n".join(lines) + "\n"

    tool_plan = planner.get("tool_execution_plan") or []
    risk_checks = planner.get("risk_checks") or []
    target_position = (context_summary or {}).get("target_position") if isinstance(context_summary, dict) else None
    account_count = (context_summary or {}).get("account_count") if isinstance(context_summary, dict) else None
    position_count = (context_summary or {}).get("position_count") if isinstance(context_summary, dict) else None

    lines = [
        "# todo",
        "",
        "## 任务识别",
        f"- [x] intent: {planner.get('intent') or '-'}",
        f"- [x] primary_symbol: {planner.get('primary_symbol') or '-'}",
        f"- [x] has_position: {planner.get('has_position')}",
        f"- [x] main_dimension: {planner.get('main_dimension') or '-'}",
        f"- [x] supporting_dimensions: {', '.join(str(item) for item in planner.get('supporting_dimensions') or []) or '-'}",
        f"- [x] expected_output: {planner.get('expected_output') or '-'}",
        f"- [x] context_accounts: {account_count if account_count is not None else '-'}",
        f"- [x] context_positions: {position_count if position_count is not None else '-'}",
    ]
    if isinstance(target_position, dict):
        lines.extend([
            f"- [x] target_quantity: {target_position.get('quantity') or '-'}",
            f"- [x] target_avg_cost: {target_position.get('avg_cost') or '-'}",
            f"- [x] target_position_pct: {target_position.get('position_pct') or '-'}",
        ])

    ledger_reuse = (
        (context_summary or {}).get("planning_ledger_reuse")
        if isinstance(context_summary, dict)
        else None
    )
    _append_planning_ledger_contract_to_todo(lines, ledger_reuse if isinstance(ledger_reuse, dict) else None)

    lines.extend(["", "## 初始假设"])
    hypotheses = planner.get("hypotheses") or []
    if hypotheses:
        for hypothesis in hypotheses:
            if isinstance(hypothesis, dict):
                lines.append(f"- [ ] {hypothesis.get('id') or '-'}: {hypothesis.get('text') or '-'}")
            else:
                lines.append(f"- [ ] {hypothesis}")
    else:
        lines.append("- [ ] 无初始假设")

    lines.extend(["", "## 维度计划"])
    main_dimension = planner.get("main_dimension")
    for capability in planner.get("capabilities") or []:
        role = "主维度" if capability == main_dimension else "辅助维度"
        lines.append(f"- [x] {role}: capability={capability}")
    if not planner.get("capabilities"):
        lines.append("- [ ] 无能力域")
    skipped_dimensions = planner.get("skipped_dimensions") or []
    for item in skipped_dimensions:
        if isinstance(item, dict):
            lines.append(f"- [x] 可省略维度: capability={item.get('capability') or '-'} reason={item.get('reason') or '-'}")
        else:
            lines.append(f"- [x] 可省略维度: {item}")

    lines.extend(["", "## 工具计划"])
    if tool_plan:
        for item in tool_plan:
            if not isinstance(item, dict):
                continue
            tools = ", ".join(str(tool) for tool in item.get("tools") or []) or "-"
            missing = ", ".join(str(tool) for tool in item.get("missing_tools") or []) or "-"
            purpose = item.get("purpose") or "-"
            lines.append(
                f"- [ ] capability={item.get('capability') or '-'} -> tools=[{tools}] "
                f"-> purpose={purpose}"
            )
            lines.append(f"  expected_result={item.get('expected_result') or '-'}")
            lines.append(f"  downstream_use={item.get('downstream_use') or '-'}")
            lines.append(f"  fallback_on_failure={item.get('fallback_on_failure') or '-'}")
            lines.append(f"  next_step={item.get('next_step') or '-'}")
            if missing != "-":
                lines.append(f"  missing_tools=[{missing}]")
    else:
        lines.append("- [ ] 无工具计划")

    lines.extend(["", "## 风险检查"])
    if risk_checks:
        for check in risk_checks:
            lines.append(f"- [ ] {check}")
    else:
        lines.append("- [ ] 无风险检查")

    missing_tools = planner.get("missing_tools") or []
    if missing_tools:
        lines.extend(["", "## 缺失工具", *[f"- [ ] {tool}" for tool in missing_tools]])

    lines.extend(["", "## 执行停止条件"])
    stop_conditions = planner.get("stop_conditions") or []
    if stop_conditions:
        lines.extend([f"- [ ] {condition}" for condition in stop_conditions])
    else:
        lines.append("- [ ] 无停止条件")

    lines.extend(["", "## Replan 策略"])
    replan_policy = planner.get("replan_policy") or {}
    if isinstance(replan_policy, dict) and replan_policy:
        mode = replan_policy.get("mode")
        if mode:
            lines.append(f"- [x] mode={mode}")
        for title, key in (
            ("触发条件", "trigger_conditions"),
            ("允许动作", "allowed_actions"),
            ("禁止动作", "forbidden_actions"),
        ):
            values = replan_policy.get(key) or []
            if values:
                lines.append(f"- [ ] {title}:")
                lines.extend([f"  - {value}" for value in values])
        if replan_policy.get("artifact_update"):
            lines.append(f"- [ ] artifact_update={replan_policy.get('artifact_update')}")
        if replan_policy.get("watchlist_note"):
            lines.append(f"- [ ] watchlist_note={replan_policy.get('watchlist_note')}")
    else:
        lines.append("- [ ] 无 replan 策略")

    _append_execute_status_to_todo(lines, tool_plan, tool_calls or [])

    return "\n".join(lines) + "\n"


def _append_planning_ledger_contract_to_todo(
    lines: List[str],
    ledger_reuse: Optional[Dict[str, Any]],
) -> None:
    source_trace = (ledger_reuse or {}).get("source_trace_id") or "-"
    reuse_status = (ledger_reuse or {}).get("reuse_status") or "new_plan"
    reusable_sections = ", ".join(str(item) for item in (ledger_reuse or {}).get("reusable_sections") or []) or "-"
    failed_or_degraded = (ledger_reuse or {}).get("failed_or_degraded_count")
    open_items = (ledger_reuse or {}).get("open_items_count")
    lines.extend(
        [
            "",
            "## Planning Ledger 复用契约",
            "- [x] ledger_name=Planning Ledger / 计划账本",
            f"- [x] reuse_source_trace={source_trace}",
            f"- [x] reuse_status={reuse_status}",
            f"- [x] reusable_sections={reusable_sections}",
            f"- [x] prior_open_items={open_items if open_items is not None else '-'}",
            f"- [x] prior_failed_or_degraded={failed_or_degraded if failed_or_degraded is not None else '-'}",
            "- [x] reuse_payload=task_identification,tool_plan,open_items,failed_or_degraded_steps,execution_status_summary",
            "- [x] reuse_rule=仅作为 delta planning 提示；实时行情、资金、新闻、市场状态和账户事实必须按新鲜度重验",
            "- [x] invalidates_on=user_goal_changed,symbol_scope_changed,planner_or_tool_contract_changed,stale_realtime_data,degraded_fallback",
        ]
    )


def _append_execute_status_to_todo(
    lines: List[str],
    tool_plan: Any,
    tool_calls: List[Dict[str, Any]],
) -> None:
    executed_tools = _summarize_executed_tools(tool_calls)
    planned_contracts = _planned_tool_contracts(tool_plan)
    lines.extend(["", "## 执行状态"])
    if tool_calls:
        for call in tool_calls:
            if not isinstance(call, dict):
                continue
            status = _tool_call_status(call)
            checkbox = "x" if status == "success" else " "
            tool_name = call.get("tool") or "-"
            duration = call.get("duration")
            result_length = call.get("result_length")
            cached = " cached" if call.get("cached") else ""
            timeout = " timeout" if call.get("timeout") else ""
            lines.append(
                f"- [{checkbox}] {tool_name}: {status}{cached}{timeout}, "
                f"duration={duration if duration is not None else '-'}s, "
                f"result_length={result_length if result_length is not None else '-'}"
            )
            arguments = call.get("arguments")
            if arguments:
                lines.append(f"  arguments={json.dumps(arguments, ensure_ascii=False, default=str)}")
            contract = planned_contracts.get(str(tool_name)) if tool_name else None
            if contract:
                lines.append(f"  planned_capability={contract.get('capability') or '-'}")
                lines.append(f"  expected_result={contract.get('expected_result') or '-'}")
                lines.append(f"  downstream_use={contract.get('downstream_use') or '-'}")
                lines.append(f"  fallback_on_failure={contract.get('fallback_on_failure') or '-'}")
                lines.append(f"  next_step={contract.get('next_step') or '-'}")
            preview = call.get("result_preview")
            if preview:
                lines.append(f"  result_preview={preview}")
    else:
        lines.append("- [ ] 尚未执行工具")

    planned_tools = _planned_tool_names(tool_plan)
    not_called = [tool for tool in planned_tools if tool not in executed_tools]
    if not_called:
        lines.extend(["", "## 未调用计划工具"])
        for tool in not_called:
            contract = planned_contracts.get(tool) or {}
            suffix = f" capability={contract.get('capability')}" if contract.get("capability") else ""
            lines.append(f"- [ ] {tool}{suffix}")
            if contract.get("purpose"):
                lines.append(f"  purpose={contract.get('purpose')}")
            if contract.get("fallback_on_failure"):
                lines.append(f"  fallback_on_failure={contract.get('fallback_on_failure')}")

    lines.extend(["", "## Execute Protocol 复核"])
    if tool_calls:
        failed = [call.get("tool") for call in tool_calls if isinstance(call, dict) and _tool_call_status(call) != "success"]
        lines.append(f"- [{'x' if not failed else ' '}] 工具失败已记录: {', '.join(str(item) for item in failed) if failed else '无'}")
        lines.append("- [x] Evidence Ledger 已进入最终报告复核")
        lines.append("- [x] 停止条件已复核")
    else:
        lines.append("- [ ] 等待工具执行后复核")


def _planned_tool_contracts(tool_plan: Any) -> Dict[str, Dict[str, Any]]:
    contracts: Dict[str, Dict[str, Any]] = {}
    for item in tool_plan or []:
        if not isinstance(item, dict):
            continue
        for tool in item.get("tools") or []:
            name = str(tool or "").strip()
            if not name or name in contracts:
                continue
            contracts[name] = {
                "capability": item.get("capability"),
                "purpose": item.get("purpose"),
                "expected_result": item.get("expected_result"),
                "downstream_use": item.get("downstream_use"),
                "fallback_on_failure": item.get("fallback_on_failure"),
                "next_step": item.get("next_step"),
                "required": item.get("required"),
            }
    return contracts


def _planned_tool_names(tool_plan: Any) -> List[str]:
    names: List[str] = []
    for item in tool_plan or []:
        if not isinstance(item, dict):
            continue
        for tool in item.get("tools") or []:
            if tool and tool not in names:
                names.append(str(tool))
    return names


def _summarize_executed_tools(tool_calls: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    executed: Dict[str, Dict[str, Any]] = {}
    for call in tool_calls:
        if not isinstance(call, dict):
            continue
        tool = str(call.get("tool") or "")
        if tool:
            executed[tool] = call
    return executed


def _tool_call_status(call: Dict[str, Any]) -> str:
    if call.get("timeout"):
        return "timeout"
    status = str(call.get("status") or "").strip().lower()
    if status == "not_supported":
        return "success"
    if _tool_call_has_errors(call) and str(call.get("status") or "").lower() != "not_supported":
        return "failed"
    if _tool_result_preview_has_errors(call.get("result_preview")):
        return "failed"
    return "success" if call.get("success") is True else "failed"


def _tool_call_has_errors(call: Dict[str, Any]) -> bool:
    if not isinstance(call, dict):
        return False
    if call.get("error"):
        return True
    errors = call.get("errors")
    if isinstance(errors, list):
        return any(str(item).strip() for item in errors)
    return bool(errors)


def _tool_result_preview_has_errors(preview: Any) -> bool:
    payload = _parse_result_preview(preview)
    return _structured_tool_payload_has_errors(payload)


def _parse_result_preview(preview: Any) -> Any:
    if isinstance(preview, (dict, list)):
        return preview
    if not isinstance(preview, str):
        return None
    text = preview.strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except (TypeError, ValueError, json.JSONDecodeError):
        return None


def _structured_tool_payload_has_errors(payload: Any) -> bool:
    if isinstance(payload, list):
        return any(_structured_tool_payload_has_errors(item) for item in payload)
    if not isinstance(payload, dict):
        return False
    status = str(payload.get("status") or "").strip().lower()
    if status == "not_supported":
        return False
    if status in {"failed", "error", "tool_failed", "timeout"}:
        return True
    if payload.get("timeout") is True:
        return True
    if payload.get("success") is False:
        return True
    if payload.get("error"):
        return True
    errors = payload.get("errors")
    if isinstance(errors, list):
        has_errors = any(str(item).strip() for item in errors)
    else:
        has_errors = bool(errors)
    if has_errors:
        return not (status in {"ok", "partial"} and _structured_tool_payload_has_effective_data(payload))
    return False


def _structured_tool_payload_has_effective_data(payload: Dict[str, Any]) -> bool:
    ignored_keys = {
        "status",
        "errors",
        "error",
        "error_summary",
        "note",
        "message",
        "stock_code",
        "code",
        "query",
        "source_chain",
        "amount_unit",
        "raw_amount_unit",
        "context_policy",
    }
    for key, value in payload.items():
        if key in ignored_keys:
            continue
        if _structured_tool_value_has_effective_data(value):
            return True
    return False


def _structured_tool_value_has_effective_data(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str) and value == "":
        return False
    if isinstance(value, dict):
        return any(_structured_tool_value_has_effective_data(item) for item in value.values())
    if isinstance(value, (list, tuple, set)):
        return any(_structured_tool_value_has_effective_data(item) for item in value)
    return True


def _normalize_tool_call_status(call: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(call, dict):
        return call
    normalized = dict(call)
    normalized["success"] = _tool_call_status(normalized) == "success"
    return normalized


def _normalize_tool_calls_status(tool_calls: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [
        _normalize_tool_call_status(call) if isinstance(call, dict) else call
        for call in tool_calls or []
    ]


def _normalize_tool_event_status(event: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(event, dict) or event.get("type") != "tool_done":
        return event
    return _normalize_tool_call_status(event)


def _build_evidence_ledger(tool_calls: List[Dict[str, Any]]) -> Dict[str, Any]:
    entries: List[Dict[str, Any]] = []
    for index, call in enumerate(tool_calls, start=1):
        if not isinstance(call, dict):
            continue
        status = _tool_call_status(call)
        limitation = _tool_call_limitation(call, status)
        entries.append({
            "index": index,
            "step": call.get("step"),
            "tool": call.get("tool") or "-",
            "arguments": call.get("arguments") or {},
            "status": status,
            "duration": call.get("duration"),
            "cached": bool(call.get("cached")),
            "evidence": call.get("result_preview") or "",
            "limitation": limitation,
            "impact": _tool_call_impact(status, limitation),
        })
    return {
        "schema_version": 1,
        "entry_count": len(entries),
        "entries": entries,
    }


def _tool_call_limitation(call: Dict[str, Any], status: str) -> str:
    if status == "timeout":
        return "工具调用超时，结果不可作为强证据。"
    if status == "failed":
        preview = call.get("result_preview") or ""
        return f"工具调用失败或返回错误，需降级使用其他证据。{preview}".strip()
    if call.get("cached"):
        return "命中缓存结果，需结合行情时间戳判断时效。"
    return "未记录额外局限；仍需结合最终报告中的数据口径和时效说明。"


def _tool_call_impact(status: str, limitation: str) -> str:
    if status == "success":
        return "该工具结果可作为最终报告的证据输入，执行器需说明它支持或削弱了哪条判断。"
    return f"该工具未提供可靠证据，执行器需在最终报告中降级结论。{limitation}"


def _build_trace_risk_gate_payload(
    *,
    result: Any,
    context: Dict[str, Any],
    tool_calls: List[Dict[str, Any]],
    composite_payload: Optional[Dict[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    """Build and evaluate the deterministic risk gate payload for a trace run."""
    agent_user_context = _coerce_trace_agent_user_context(context)
    if agent_user_context is None:
        return None

    primary_symbol = _resolve_trace_primary_symbol(
        context,
        agent_user_context,
        result=result,
        tool_calls=tool_calls,
    )
    if not primary_symbol:
        return None
    if (
        isinstance(composite_payload, dict)
        and composite_payload.get("error_code") == "target_symbol_mismatch"
    ):
        return _trace_risk_gate_target_mismatch_payload(
            expected_symbol=str(composite_payload.get("expected_symbol") or primary_symbol),
            actual_symbol=str(composite_payload.get("actual_symbol") or ""),
            source="composite_verdict",
            observed_tool_symbols=(
                composite_payload.get("observed_tool_symbols")
                if isinstance(composite_payload.get("observed_tool_symbols"), list)
                else _trace_tool_target_symbols(tool_calls)
            ),
        )

    price_plan_result = _build_trace_price_plan_result(
        primary_symbol=primary_symbol,
        context=context,
        result=result,
        tool_calls=tool_calls,
        composite_payload=composite_payload,
    )
    price_plan = price_plan_result.price_plan
    trade_plan = _project_price_plan_to_legacy_trade_plan(
        price_plan=price_plan,
        result=result,
        context=context,
        agent_user_context=agent_user_context,
        selected_scenario_id=(
            price_plan_result.account_decision.selected_scenario_id
            if price_plan_result.account_decision is not None
            else None
        ),
        account_decision=price_plan_result.account_decision,
    )

    try:
        from src.agent.risk_gate import QuoteState, RiskGateEvaluator, RiskGateInput

        position = _find_trace_position(agent_user_context, primary_symbol)
        account = _find_trace_account(agent_user_context, position)
        quote = _build_trace_quote_state(primary_symbol, tool_calls, fallback_position=position)
        composite_verdict = (
            composite_payload.get("composite_verdict")
            if isinstance(composite_payload, dict)
            and isinstance(composite_payload.get("composite_verdict"), dict)
            else None
        )
        trade_plan, composite_clamp_applied, composite_pre_clamp = _clamp_trace_trade_plan(
            trade_plan,
            composite_verdict,
        )
        gate_input = RiskGateInput(
            plan=trade_plan,
            quote=quote or QuoteState(symbol=primary_symbol),
            investor=agent_user_context.investor,
            account=account,
            position=position,
            data_quality=_trace_data_quality(tool_calls),
            failed_tools=_trace_failed_tools(tool_calls),
            l3_confidence=_extract_trace_l3_confidence(result),
            current_total_exposure_pct=_current_total_exposure_pct(agent_user_context),
            composite_verdict=composite_verdict,
            composite_clamp_applied=composite_clamp_applied,
            composite_pre_clamp_target_position_pct=composite_pre_clamp,
        )
        gate_result = RiskGateEvaluator().evaluate(gate_input)
        price_plan_result = _finalize_trace_price_plan_result(
            price_plan_result,
            gate_result=gate_result,
            result=result,
            context=context,
        )
        return {
            "schema_version": 1,
            "price_plan": price_plan.model_dump(mode="json"),
            "trade_plan_result": price_plan_result.model_dump(mode="json"),
            "trade_plan": trade_plan.model_dump(mode="json"),
            "risk_gate": gate_result.model_dump(mode="json"),
            "quote": quote.__dict__ if quote is not None else None,
            "source": "deterministic_price_plan",
        }
    except Exception as exc:
        logger.warning("Agent trace risk gate build failed: %s", exc, exc_info=True)
        return {
            "schema_version": 1,
            "error": str(exc),
            "price_plan": price_plan.model_dump(mode="json"),
            "trade_plan_result": price_plan_result.model_dump(mode="json"),
            "trade_plan": trade_plan.model_dump(mode="json"),
        }


def _build_trace_price_plan_result(
    *,
    primary_symbol: str,
    context: Dict[str, Any],
    result: Any,
    tool_calls: List[Dict[str, Any]],
    composite_payload: Optional[Dict[str, Any]] = None,
) -> Any:
    from src.agent.pricing.runtime import enrich_trade_plan_result, generate_trade_plan
    from src.agent.pricing.schemas import TradePlanResult
    from src.services.history_loader import get_frozen_target_date

    existing = getattr(result, "trade_plan_result", None)
    if existing is None:
        existing = context.get("pre_debate_price_plan_result")
    price_plan_result = None
    if existing is not None:
        try:
            price_plan_result = (
                existing
                if isinstance(existing, TradePlanResult)
                else TradePlanResult.model_validate(existing)
            )
        except Exception:
            pass
    if price_plan_result is None:
        as_of = (
            context.get("as_of")
            or _trace_composite_as_of(primary_symbol, tool_calls)
            or get_frozen_target_date()
            or datetime.now().date()
        )
        if isinstance(as_of, datetime):
            as_of = as_of.date()
        elif not isinstance(as_of, date):
            as_of = datetime.fromisoformat(str(as_of)[:10]).date()
        trace_ref = str(context.get("session_id") or context.get("trace_id") or "").strip() or None
        price_plan_result = generate_trade_plan(
            primary_symbol,
            as_of=as_of,
            trace_ref=trace_ref,
            persist=True,
        )
    decision_context = dict(context)
    decision_context["price_plan_composite_payload"] = composite_payload
    price_plan_result = enrich_trade_plan_result(
        price_plan_result,
        agent_result=result,
        context=decision_context,
    )
    try:
        setattr(result, "price_plan", price_plan_result.price_plan.model_dump(mode="json"))
        setattr(result, "trade_plan_result", price_plan_result.model_dump(mode="json"))
        from src.agent.pricing.report import merge_price_plan_into_report

        setattr(
            result,
            "content",
            merge_price_plan_into_report(
                str(getattr(result, "content", "") or ""),
                price_plan_result,
            ),
        )
    except Exception:
        pass
    return price_plan_result


def _project_price_plan_to_legacy_trade_plan(
    *,
    price_plan: Any,
    result: Any,
    context: Dict[str, Any],
    agent_user_context: AgentUserContext,
    selected_scenario_id: Optional[str] = None,
    account_decision: Any = None,
) -> TradePlan:
    from src.agent.pricing.schemas import ScenarioRole

    scenarios = list(getattr(price_plan, "scenarios", []) or [])
    primary = next(
        (item for item in scenarios if selected_scenario_id and item.scenario_id == selected_scenario_id),
        None,
    ) or next((item for item in scenarios if item.role is ScenarioRole.PRIMARY), None)
    llm_action = _extract_trace_action(result) or "wait"
    pre_gate_entry_allowed = bool(
        account_decision is not None
        and account_decision.execution_status == "risk_blocked"
        and account_decision.reason_codes == ["RISK_GATE_PENDING"]
        and account_decision.target_position_pct is not None
        and account_decision.target_position_pct > 0
    )
    if llm_action in {"open", "add"} and primary is not None and pre_gate_entry_allowed:
        action = llm_action
        current_action = llm_action
    elif llm_action in {"open", "add"}:
        action = "wait"
        current_action = "wait"
    else:
        action = llm_action
        current_action = llm_action
    zones = [
        {
            "label": {
                "primary": "主入场区",
                "secondary": "次入场区",
                "observation": "观察区",
            }[item.role.value],
            "low": float(item.entry_low),
            "high": float(item.entry_high),
            "rationale": (
                f"{','.join(item.source_families)}; "
                f"scenario={item.scenario_id}; role={item.role.value}"
            ),
        }
        for item in scenarios
    ]
    target_position_pct = (
        float(account_decision.target_position_pct)
        if account_decision is not None and account_decision.target_position_pct is not None
        else None
    )
    return TradePlan(
        symbol=price_plan.symbol,
        action=action,
        current_action=current_action,
        order_type="manual",
        target_position_pct=target_position_pct,
        entry_zone_low=float(primary.entry_low) if primary is not None else None,
        entry_zone_high=float(primary.entry_high) if primary is not None else None,
        entry_zones=zones,
        stop_loss_price=float(primary.stop_loss) if primary and primary.stop_loss else None,
        take_profit_price=float(primary.target_1) if primary and primary.target_1 else None,
        invalidation_conditions=(
            [rule.rule_type for rule in primary.invalidation_rules]
            if primary is not None
            else []
        ),
        execution_window=(
            f"confirmation_deadline={primary.confirmation_deadline.isoformat()}; "
            f"confirmation_days={primary.confirmation_valid_trading_days}; "
            f"order_days={primary.order_valid_trading_days}"
            if primary is not None
            else None
        ),
        review_triggers=_extract_trace_review_triggers(result),
        notes="Compatibility projection from deterministic PricePlanArtifact.",
        metadata={
            "price_source": "deterministic_price_plan",
            "plan_id": price_plan.plan_id,
            "selected_scenario_id": primary.scenario_id if primary else None,
            "generation_status": price_plan.generation_status.value,
            "analysis_mode": agent_user_context.report.analysis_mode,
            "intent": agent_user_context.report.intent,
            "stock_name": context.get("stock_name"),
        },
    )


def _finalize_trace_price_plan_result(
    price_plan_result: Any,
    *,
    gate_result: Any,
    result: Any,
    context: Optional[Dict[str, Any]] = None,
) -> Any:
    decision = getattr(price_plan_result, "account_decision", None)
    if decision is None:
        return price_plan_result
    from src.agent.pricing.decision import finalize_account_decision
    from src.agent.pricing.report import merge_price_plan_into_report
    from src.agent.pricing.repository import PricePlanRepository
    from src.agent.pricing.runtime import _decision_identity

    final_decision = finalize_account_decision(decision, risk_gate_result=gate_result)
    owner_id, account_id, _ = _decision_identity(context)
    final_decision = PricePlanRepository().save_account_decision(
        final_decision,
        owner_id=owner_id,
        account_id=account_id,
    )
    reason_codes = sorted(
        set(
            code
            for code in [*price_plan_result.reason_codes, *final_decision.reason_codes]
            if code != "RISK_GATE_PENDING"
        )
    )
    final_result = price_plan_result.model_copy(
        update={
            "account_decision": final_decision,
            "execution_status": final_decision.execution_status,
            "reason_codes": reason_codes,
        }
    )
    try:
        result.price_plan = final_result.price_plan.model_dump(mode="json")
        result.trade_plan_result = final_result.model_dump(mode="json")
        result.content = merge_price_plan_into_report(
            str(getattr(result, "content", "") or ""),
            final_result,
        )
    except Exception:
        pass
    return final_result


def _clamp_trace_trade_plan(
    trade_plan: Any,
    composite_verdict: Optional[Dict[str, Any]],
) -> tuple[Any, bool, Optional[float]]:
    """Apply the composite cap once before constructing RiskGateInput."""
    if not isinstance(composite_verdict, dict) or composite_verdict.get("shadow_mode", True):
        return trade_plan, False, None
    raw_cap = composite_verdict.get("target_position_cap_pct")
    try:
        cap = float(raw_cap) if raw_cap is not None else None
    except (TypeError, ValueError):
        cap = None
    if cap is not None and 0 <= cap <= 1:
        cap *= 100.0
    target = getattr(trade_plan, "target_position_pct", None)
    if cap is None or target is None or target <= cap:
        return trade_plan, False, target
    return trade_plan.model_copy(update={"target_position_pct": cap}), True, target


def _build_trace_composite_verdict_payload(
    *,
    result: Any,
    context: Dict[str, Any],
    tool_calls: List[Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    """Build one shadow verdict for a single-stock trace."""
    agent_user_context = _coerce_trace_agent_user_context(context)
    if agent_user_context is None:
        return None
    primary_symbol = _resolve_trace_primary_symbol(
        context,
        agent_user_context,
        result=result,
        tool_calls=tool_calls,
    )
    if not primary_symbol:
        return None

    tool_symbols = _trace_tool_target_symbols(tool_calls)
    if len(tool_symbols) == 1 and tool_symbols[0] != _normalize_trace_symbol(primary_symbol):
        return _trace_target_mismatch_payload(
            expected_symbol=primary_symbol,
            actual_symbol=tool_symbols[0],
            source="tool_calls",
            observed_tool_symbols=tool_symbols,
        )

    prefetched = context.get("trace_composite_verdict")
    if isinstance(prefetched, dict) and prefetched.get("status") != "failed":
        prefetched_symbol = _trace_composite_payload_symbol(prefetched)
        if prefetched_symbol and prefetched_symbol != _normalize_trace_symbol(primary_symbol):
            return _trace_target_mismatch_payload(
                expected_symbol=primary_symbol,
                actual_symbol=prefetched_symbol,
                source="prefetched_composite",
                observed_tool_symbols=tool_symbols,
            )
        market_regime = _trace_market_regime(tool_calls)
        snapshot_payload = prefetched.get("snapshot")
        if market_regime is not None and isinstance(snapshot_payload, dict):
            snapshot = CompositeScoreSnapshot.model_validate(snapshot_payload)
            verdict = evaluate_composite_verdict(snapshot, market_regime=market_regime)
            return {
                **prefetched,
                "composite_verdict": verdict.model_dump(mode="json"),
            }
        return prefetched

    selection_payload = _trace_selection_composite_payload(result, primary_symbol)
    if selection_payload is not None:
        selection_symbol = _trace_composite_payload_symbol(selection_payload)
        if selection_symbol and selection_symbol != _normalize_trace_symbol(primary_symbol):
            return _trace_target_mismatch_payload(
                expected_symbol=primary_symbol,
                actual_symbol=selection_symbol,
                source="stock_selection",
                observed_tool_symbols=tool_symbols,
            )
        return selection_payload
    if isinstance(getattr(result, "stock_selection", None), dict):
        return None

    try:
        from src.services.history_loader import get_frozen_target_date

        as_of = _trace_composite_as_of(primary_symbol, tool_calls) or get_frozen_target_date() or datetime.now().date()
        payload = score_symbol_composite(
            primary_symbol,
            as_of=as_of,
            quote_basis="latest_trading_day",
            holding=_find_trace_position(agent_user_context, primary_symbol) is not None,
            snapshot_builder=CompositeSnapshotBuilder(fallback_to_network=True),
            market_regime=_trace_market_regime(tool_calls),
        )
        actual_symbol = _trace_composite_payload_symbol(payload)
        if actual_symbol and actual_symbol != _normalize_trace_symbol(primary_symbol):
            return _trace_target_mismatch_payload(
                expected_symbol=primary_symbol,
                actual_symbol=actual_symbol,
                source="runtime_composite",
                observed_tool_symbols=tool_symbols,
            )
        return payload
    except Exception as exc:
        logger.warning("Agent trace composite shadow scoring failed for %s: %s", primary_symbol, exc)
        return {
            "schema_version": "stock-composite-shadow.v1",
            "status": "failed",
            "symbol": primary_symbol,
            "error": str(exc),
        }


def _trace_composite_payload_symbol(payload: Dict[str, Any]) -> str:
    snapshot = payload.get("snapshot") if isinstance(payload.get("snapshot"), dict) else {}
    verdict = (
        payload.get("composite_verdict")
        if isinstance(payload.get("composite_verdict"), dict)
        else {}
    )
    return _normalize_trace_symbol(
        snapshot.get("symbol") or verdict.get("symbol") or payload.get("symbol")
    )


def _trace_target_mismatch_payload(
    *,
    expected_symbol: str,
    actual_symbol: str,
    source: str,
    observed_tool_symbols: Optional[List[str]] = None,
) -> Dict[str, Any]:
    expected = _normalize_trace_symbol(expected_symbol)
    actual = _normalize_trace_symbol(actual_symbol)
    logger.error(
        "Agent trace composite target mismatch: expected=%s actual=%s source=%s tool_symbols=%s",
        expected,
        actual,
        source,
        observed_tool_symbols or [],
    )
    return {
        "schema_version": "stock-composite-shadow.v1",
        "status": "failed",
        "error_code": "target_symbol_mismatch",
        "error": f"确定性评分目标不一致：请求 {expected or '-'}，实际 {actual or '-'}。已拒绝使用该评分。",
        "expected_symbol": expected or None,
        "actual_symbol": actual or None,
        "mismatch_source": source,
        "observed_tool_symbols": observed_tool_symbols or [],
    }


def _quarantine_stored_trace_target_mismatch(
    *,
    context: Dict[str, Any],
    tool_calls: List[Dict[str, Any]],
    composite_payload: Any,
    risk_gate_payload: Any,
    expected_symbol: Optional[str] = None,
) -> tuple[Any, Any]:
    """Reject persisted scoring artifacts that belong to a different security."""
    agent_user_context = _coerce_trace_agent_user_context(context)
    if agent_user_context is None and not expected_symbol:
        return composite_payload, risk_gate_payload

    expected_symbol = expected_symbol or _resolve_trace_primary_symbol(
        context,
        agent_user_context,
        tool_calls=tool_calls,
    )
    if not expected_symbol:
        return composite_payload, risk_gate_payload

    tool_symbols = _trace_tool_target_symbols(tool_calls)
    actual_composite = (
        _trace_composite_payload_symbol(composite_payload)
        if isinstance(composite_payload, dict)
        else ""
    )
    if actual_composite and actual_composite != _normalize_trace_symbol(expected_symbol):
        composite_payload = _trace_target_mismatch_payload(
            expected_symbol=expected_symbol,
            actual_symbol=actual_composite,
            source="stored_artifact",
            observed_tool_symbols=tool_symbols,
        )

    actual_gate = ""
    if isinstance(risk_gate_payload, dict):
        trade_plan = risk_gate_payload.get("trade_plan")
        quote = risk_gate_payload.get("quote")
        if isinstance(trade_plan, dict):
            actual_gate = _normalize_trace_symbol(trade_plan.get("symbol"))
        if not actual_gate and isinstance(quote, dict):
            actual_gate = _normalize_trace_symbol(quote.get("symbol"))
    if actual_gate and actual_gate != _normalize_trace_symbol(expected_symbol):
        risk_gate_payload = _trace_risk_gate_target_mismatch_payload(
            expected_symbol=expected_symbol,
            actual_symbol=actual_gate,
            source="stored_artifact",
            observed_tool_symbols=tool_symbols,
        )
    return composite_payload, risk_gate_payload


def _trace_risk_gate_target_mismatch_payload(
    *,
    expected_symbol: str,
    actual_symbol: str,
    source: str,
    observed_tool_symbols: Optional[List[str]] = None,
) -> Dict[str, Any]:
    expected = _normalize_trace_symbol(expected_symbol)
    actual = _normalize_trace_symbol(actual_symbol)
    logger.error(
        "Agent trace risk gate target mismatch: expected=%s actual=%s source=%s tool_symbols=%s",
        expected,
        actual,
        source,
        observed_tool_symbols or [],
    )
    return {
        "schema_version": 1,
        "status": "failed",
        "error_code": "target_symbol_mismatch",
        "error": f"风险门目标不一致：请求 {expected or '-'}，实际 {actual or '-'}。已拒绝使用该风险门结果。",
        "expected_symbol": expected or None,
        "actual_symbol": actual or None,
        "mismatch_source": source,
        "observed_tool_symbols": observed_tool_symbols or [],
    }


def _trace_selection_composite_payload(result: Any, primary_symbol: str) -> Optional[Dict[str, Any]]:
    stock_selection = getattr(result, "stock_selection", None) if result is not None else None
    if not isinstance(stock_selection, dict):
        return None
    final_report = stock_selection.get("final_report_json")
    if not isinstance(final_report, dict):
        return None
    stage = final_report.get("deterministic_scoring")
    full = stage.get("full") if isinstance(stage, dict) and isinstance(stage.get("full"), dict) else {}
    results = full.get("results") if isinstance(full.get("results"), list) else []
    normalized_target = _normalize_trace_symbol(primary_symbol)
    for item in results:
        if not isinstance(item, dict) or _normalize_trace_symbol(item.get("code")) != normalized_target:
            continue
        payload = dict(item)
        payload.pop("code", None)
        return payload
    return None


def _trace_composite_as_of(primary_symbol: str, tool_calls: List[Dict[str, Any]]) -> Optional[Any]:
    structure = _latest_tool_payload(tool_calls, "analyze_price_structure", stock_code=primary_symbol) or {}
    latest_bar = structure.get("latest_bar") if isinstance(structure.get("latest_bar"), dict) else {}
    for key in ("time", "date", "datetime"):
        if latest_bar.get(key):
            return latest_bar[key]

    history = _latest_tool_payload(tool_calls, "get_daily_history", stock_code=primary_symbol) or {}
    rows = history.get("data") if isinstance(history.get("data"), list) else []
    if rows and isinstance(rows[-1], dict):
        for key in ("date", "time", "datetime"):
            if rows[-1].get(key):
                return rows[-1][key]
    return None


def _trace_market_regime(tool_calls: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    payload = _latest_tool_payload(tool_calls, "detect_market_regime")
    if not isinstance(payload, dict):
        return None
    return {
        "regime": payload.get("regime"),
        "volatility_bucket": payload.get("volatility_bucket"),
    }


def _coerce_trace_agent_user_context(context: Dict[str, Any]) -> Optional[AgentUserContext]:
    try:
        from src.agent.executor import _coerce_agent_user_context

        return _coerce_agent_user_context(context.get("agent_user_context"))
    except Exception as exc:
        logger.debug("Agent trace risk gate context coercion failed: %s", exc)
        return None


def _resolve_trace_primary_symbol(
    context: Dict[str, Any],
    agent_user_context: AgentUserContext,
    *,
    result: Any = None,
    tool_calls: Optional[List[Dict[str, Any]]] = None,
) -> Optional[str]:
    report = agent_user_context.report
    explicit_candidates = [
        context.get("stock_code"),
        report.primary_symbol,
        *(report.target_symbols or []),
    ]
    explicit_symbols = _trace_unique_symbols(explicit_candidates)
    if report.intent == "watchlist_scan" or isinstance(getattr(result, "stock_selection", None), dict):
        if len(explicit_symbols) == 1:
            return explicit_symbols[0]
        return _extract_trace_stock_selection_symbol(result)

    if explicit_symbols:
        return explicit_symbols[0]

    tool_symbols = _trace_tool_target_symbols(tool_calls or [])
    if len(tool_symbols) == 1:
        return tool_symbols[0]

    sidecar = getattr(result, "trade_plan", None)
    sidecar_symbols = _trace_unique_symbols([
        sidecar.get("symbol") if isinstance(sidecar, dict) else None,
    ])
    return sidecar_symbols[0] if len(sidecar_symbols) == 1 else None


def _trace_tool_target_symbols(tool_calls: List[Dict[str, Any]]) -> List[str]:
    """Return unique security symbols explicitly used by single-stock tools."""
    candidates: List[Any] = []
    for call in tool_calls or []:
        if not isinstance(call, dict):
            continue
        arguments = call.get("arguments")
        if not isinstance(arguments, dict):
            continue
        candidates.extend((arguments.get("stock_code"), arguments.get("symbol")))
    return _trace_unique_symbols(candidates)


def _extract_trace_stock_selection_symbol(result: Any) -> Optional[str]:
    stock_selection = getattr(result, "stock_selection", None) if result is not None else None
    if not isinstance(stock_selection, dict):
        return None
    final_report = stock_selection.get("final_report_json") or {}
    if not isinstance(final_report, dict):
        return None
    allocation = final_report.get("portfolio_allocation") or {}
    if not isinstance(allocation, dict):
        return None
    full = allocation.get("full") or {}
    if not isinstance(full, dict):
        return None
    positions = full.get("positions_plan") or []
    if not isinstance(positions, list):
        return None
    symbols = _trace_unique_symbols(
        item.get("code") or item.get("stock_code")
        for item in positions
        if isinstance(item, dict)
    )
    return symbols[0] if len(symbols) == 1 else None


def _trace_unique_symbols(values: Any) -> List[str]:
    symbols: List[str] = []
    for value in values or []:
        normalized = _normalize_trace_symbol(value)
        if normalized and normalized not in symbols:
            symbols.append(normalized)
    return symbols


def _normalize_trace_symbol(value: Any) -> str:
    text = str(value or "").strip().upper()
    if not text:
        return ""
    if "." in text:
        prefix, suffix = text.split(".", 1)
        if prefix.isdigit():
            text = prefix
        elif suffix.isdigit():
            text = suffix
    if len(text) > 2 and text[:2] in {"SH", "SZ", "BJ"} and text[2:].isdigit():
        text = text[2:]
    return text


def _extract_trace_action(result: Any) -> Optional[TradeAction]:
    debate = getattr(result, "debate", None) if result is not None else None
    candidates: List[Any] = []
    dashboard = getattr(result, "dashboard", None) if result is not None else None
    if isinstance(dashboard, dict):
        candidates.extend(_trace_dashboard_values(
            dashboard,
            {"decision_type", "operation_advice", "final_action", "action"},
        ))
    if isinstance(debate, dict):
        judge = debate.get("judge_decision") or {}
        candidates.extend([
            judge.get("final_action"),
            judge.get("judge_action"),
            judge.get("action"),
        ])
    stock_selection = getattr(result, "stock_selection", None) if result is not None else None
    if isinstance(stock_selection, dict):
        final_report = stock_selection.get("final_report_json") or {}
        summary = final_report.get("summary") if isinstance(final_report, dict) else {}
        if isinstance(summary, dict):
            candidates.append(summary.get("final_action"))
        selection_context = stock_selection.get("selection_context") or {}
        if isinstance(selection_context, dict):
            stages = selection_context.get("stages") or {}
            judge_stage = stages.get("judge_decision") if isinstance(stages, dict) else {}
            if isinstance(judge_stage, dict):
                stage_summary = judge_stage.get("summary") or {}
                if isinstance(stage_summary, dict):
                    candidates.append(stage_summary.get("final_action"))

    for candidate in candidates:
        action = _normalize_trace_trade_action(candidate)
        if action:
            return action
    return "wait"


def _normalize_trace_trade_action(value: Any) -> Optional[TradeAction]:
    text = str(value or "").strip().lower()
    mapping: Dict[str, TradeAction] = {
        "buy": "open",
        "strong_buy": "open",
        "open_position": "open",
        "entry": "open",
        "add_position": "add",
        "increase": "add",
        "trim": "reduce",
        "take_profit": "reduce",
        "clear": "sell",
        "stop_loss": "sell",
        "no_trade": "wait",
        "watch": "monitor",
        "review": "manual_review",
        "买入": "open",
        "开仓": "open",
        "加仓": "add",
        "减仓": "reduce",
        "卖出": "sell",
        "清仓": "sell",
        "持有": "hold",
        "观望": "wait",
    }
    text = mapping.get(text, text)
    allowed = {"open", "add", "reduce", "sell", "hold", "wait", "monitor", "manual_review", "reject"}
    return text if text in allowed else None  # type: ignore[return-value]


def _trace_dashboard_values(payload: Dict[str, Any], keys: set[str]) -> List[Any]:
    values: List[Any] = []
    for key, value in payload.items():
        if key in keys and value not in (None, ""):
            if isinstance(value, dict) and key == "operation_advice":
                values.extend(item for item in value.values() if item not in (None, ""))
            else:
                values.append(value)
        if isinstance(value, dict):
            values.extend(_trace_dashboard_values(value, keys))
        elif isinstance(value, list):
            for item in value:
                if isinstance(item, dict):
                    values.extend(_trace_dashboard_values(item, keys))
    return values


def _extract_trace_review_triggers(result: Any) -> List[str]:
    debate = getattr(result, "debate", None) if result is not None else None
    if not isinstance(debate, dict):
        return []
    judge = debate.get("judge_decision") or {}
    return _string_list([judge.get("review_triggers"), judge.get("next_review"), judge.get("risk_controls")])


def _find_trace_position(agent_user_context: AgentUserContext, symbol: str) -> Any:
    normalized = symbol.strip().lower()
    for position in agent_user_context.positions:
        if str(position.symbol or "").strip().lower() == normalized and position.quantity > 0:
            return position
    return None


def _find_trace_account(agent_user_context: AgentUserContext, position: Any) -> Any:
    if position is not None and getattr(position, "account_id", None) is not None:
        for account in agent_user_context.accounts:
            if account.account_id == position.account_id:
                return account
    return agent_user_context.accounts[0] if agent_user_context.accounts else None


def _current_total_exposure_pct(agent_user_context: AgentUserContext) -> Optional[float]:
    values = [position.position_pct for position in agent_user_context.positions if position.position_pct is not None]
    if values:
        return float(sum(values))
    accounts = agent_user_context.accounts
    if len(accounts) == 1 and accounts[0].total_equity and accounts[0].total_market_value is not None:
        return float(accounts[0].total_market_value) / float(accounts[0].total_equity) * 100.0
    return None


def _build_trace_quote_state(primary_symbol: str, tool_calls: List[Dict[str, Any]], *, fallback_position: Any) -> Any:
    try:
        from src.agent.risk_gate import QuoteState

        quote_payload = _latest_tool_payload(tool_calls, "get_realtime_quote", stock_code=primary_symbol) or {}
        last_price = _to_float(
            quote_payload.get("price")
            or quote_payload.get("last_price")
            or quote_payload.get("current_price")
            or getattr(fallback_position, "last_price", None)
        )
        pct_change = _to_float(
            quote_payload.get("pct_change")
            or quote_payload.get("change_pct")
            or quote_payload.get("涨跌幅")
        )
        return QuoteState(
            symbol=primary_symbol,
            last_price=last_price,
            pct_change=pct_change,
            is_limit_up=_truthy(quote_payload.get("is_limit_up") or quote_payload.get("limit_up")),
            is_limit_down=_truthy(quote_payload.get("is_limit_down") or quote_payload.get("limit_down")),
            is_st=_truthy(quote_payload.get("is_st") or quote_payload.get("st")),
            is_delisting=_truthy(quote_payload.get("is_delisting") or quote_payload.get("delisting")),
            is_ipo_special_period=_truthy(
                quote_payload.get("is_ipo_special_period") or quote_payload.get("ipo_special_period")
            ),
            market=str(quote_payload.get("market") or "cn"),
        )
    except Exception:
        return None


def _latest_tool_payload(
    tool_calls: List[Dict[str, Any]],
    tool_name: str,
    *,
    stock_code: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    normalized_target = _normalize_trace_symbol(stock_code)
    for call in reversed(tool_calls or []):
        if not isinstance(call, dict) or call.get("tool") != tool_name:
            continue
        payload = call.get("result_json")
        if not isinstance(payload, dict):
            payload = _parse_result_preview(call.get("result_preview"))
        if not isinstance(payload, dict):
            continue
        if normalized_target:
            candidate_symbols = _trace_unique_symbols([
                call.get("arguments", {}).get("stock_code") if isinstance(call.get("arguments"), dict) else None,
                call.get("arguments", {}).get("symbol") if isinstance(call.get("arguments"), dict) else None,
                payload.get("code"),
                payload.get("stock_code"),
                payload.get("symbol"),
            ])
            if normalized_target not in candidate_symbols:
                continue
        if isinstance(payload, dict):
            return payload
    return None


def _trace_data_quality(tool_calls: List[Dict[str, Any]]) -> str:
    if not tool_calls:
        return "unknown"
    failed = _trace_failed_tools(tool_calls)
    if failed:
        return "failed"
    return "sufficient"


def _trace_failed_tools(tool_calls: List[Dict[str, Any]]) -> List[str]:
    failed: List[str] = []
    for call in tool_calls or []:
        if isinstance(call, dict) and _tool_call_status(call) != "success":
            tool = str(call.get("tool") or "")
            if tool and tool not in failed:
                failed.append(tool)
    return failed


def _extract_trace_l3_confidence(result: Any) -> Optional[float]:
    debate = getattr(result, "debate", None) if result is not None else None
    if not isinstance(debate, dict):
        return None
    judge = debate.get("judge_decision") or {}
    for key in ("confidence_after_debate", "confidence", "score"):
        value = _to_float(judge.get(key))
        if value is not None:
            return value / 100.0 if value > 1 else value
    return None


def _string_list(values: List[Any]) -> List[str]:
    result: List[str] = []
    for value in values:
        items = value if isinstance(value, list) else [value]
        for item in items:
            text = str(item or "").strip()
            if text and text not in result:
                result.append(text)
    return result


def _to_float(value: Any) -> Optional[float]:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _to_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "y", "是", "涨停", "跌停"}


def _read_llm_telemetry_events(path: Path, *, limit: int = 500) -> List[Dict[str, Any]]:
    return _read_trace_events(path / "llm_usage.jsonl", limit=limit)


def _build_llm_telemetry_summary(path: Path) -> Dict[str, Any]:
    events = _read_llm_telemetry_events(path)
    by_stage: Dict[str, Dict[str, Any]] = {}
    ok_calls = 0
    failed_calls = 0
    input_tokens = 0
    output_tokens = 0
    total_tokens = 0
    total_latency_ms = 0.0
    estimated_cost = 0.0

    for event in events:
        ok = event.get("ok") is True
        ok_calls += 1 if ok else 0
        failed_calls += 0 if ok else 1
        stage = str(event.get("stage") or "unknown")
        input_value = _to_int(event.get("input_tokens"))
        output_value = _to_int(event.get("output_tokens"))
        total_value = _to_int(event.get("total_tokens")) or input_value + output_value
        latency_value = _to_float(event.get("latency_ms")) or 0.0
        cost_value = _to_float(event.get("estimated_cost")) or 0.0
        input_tokens += input_value
        output_tokens += output_value
        total_tokens += total_value
        total_latency_ms += latency_value
        estimated_cost += cost_value
        stage_summary = by_stage.setdefault(stage, {
            "stage": stage,
            "calls": 0,
            "ok_calls": 0,
            "failed_calls": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "total_tokens": 0,
            "latency_ms": 0.0,
            "estimated_cost": 0.0,
        })
        stage_summary["calls"] += 1
        stage_summary["ok_calls"] += 1 if ok else 0
        stage_summary["failed_calls"] += 0 if ok else 1
        stage_summary["input_tokens"] += input_value
        stage_summary["output_tokens"] += output_value
        stage_summary["total_tokens"] += total_value
        stage_summary["latency_ms"] = round(float(stage_summary["latency_ms"]) + latency_value, 2)
        stage_summary["estimated_cost"] = round(float(stage_summary["estimated_cost"]) + cost_value, 8)

    total_calls = len(events)
    return {
        "schema_version": 1,
        "events": events[-200:],
        "total_calls": total_calls,
        "ok_calls": ok_calls,
        "failed_calls": failed_calls,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": total_tokens,
        "total_latency_ms": round(total_latency_ms, 2),
        "avg_latency_ms": round(total_latency_ms / total_calls, 2) if total_calls else 0.0,
        "estimated_cost": round(estimated_cost, 8),
        "by_stage": sorted(by_stage.values(), key=lambda item: str(item.get("stage") or "")),
    }


def _build_judge_sanity_summary(stock_selection: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not isinstance(stock_selection, dict):
        return None
    final_report_json = stock_selection.get("final_report_json") if isinstance(stock_selection.get("final_report_json"), dict) else {}
    selection_context = stock_selection.get("selection_context") if isinstance(stock_selection.get("selection_context"), dict) else {}
    stages = selection_context.get("stages") if isinstance(selection_context.get("stages"), dict) else {}
    judge_stage = (
        final_report_json.get("judge_decision")
        if isinstance(final_report_json.get("judge_decision"), dict)
        else stages.get("judge_decision") if isinstance(stages.get("judge_decision"), dict) else {}
    )
    if not isinstance(judge_stage, dict) or not judge_stage:
        return None
    full = judge_stage.get("full") if isinstance(judge_stage.get("full"), dict) else {}
    summary = judge_stage.get("summary") if isinstance(judge_stage.get("summary"), dict) else {}
    sanity_checks = full.get("sanity_checks") if isinstance(full.get("sanity_checks"), list) else []
    required_plan_changes = (
        full.get("required_plan_changes")
        if isinstance(full.get("required_plan_changes"), list)
        else []
    )
    final_action = (
        summary.get("final_action")
        or full.get("final_action")
        or full.get("action")
        or ""
    )
    primary_plan_verdict = full.get("primary_plan_verdict") or summary.get("primary_plan_verdict") or ""
    return {
        "schema_version": 1,
        "final_action": final_action,
        "primary_plan_verdict": primary_plan_verdict,
        "decision_summary": summary.get("decision_summary") or full.get("decision_summary") or "",
        "reason": full.get("reason") or summary.get("reason") or "",
        "check_count": len(sanity_checks),
        "required_change_count": len(required_plan_changes),
        "sanity_checks": sanity_checks,
        "required_plan_changes": required_plan_changes,
        "summary": summary,
    }


class TraceArtifactWriter:
    """Best-effort file writer for developer trace artifacts."""

    def __init__(self, session_id: str):
        self.session_id = session_id
        self.path = _trace_artifact_root() / _safe_trace_session_dir_name(session_id)
        self._initialized = False
        self._events: List[Dict[str, Any]] = []

    def initialize(self, *, request: AgentTraceRunRequest, context: Dict[str, Any]) -> None:
        self.path.mkdir(parents=True, exist_ok=True)
        self._initialized = True
        context_summary = _build_trace_context_summary(context)
        planner = _build_planner_trace(context)
        _write_trace_json(
            self.path / "request.json",
            request.model_dump(mode="json"),
        )
        _write_trace_json(
            self.path / "context.json",
            {
                "context": _jsonable_trace_context(context),
                "context_summary": context_summary,
                "agent_user_context": _serialize_agent_user_context(context.get("agent_user_context")),
            },
        )
        _write_trace_json(self.path / "planner.json", planner)
        (self.path / "todo.md").write_text(_planner_to_todo_md(planner, context_summary), encoding="utf-8")
        (self.path / "events.ndjson").write_text("", encoding="utf-8")

    def append_event(self, event: Dict[str, Any]) -> None:
        if not self._initialized:
            return
        stored_event = dict(event)
        stored_event.pop("__artifact_payload", None)
        self._events.append(stored_event)
        _append_trace_event(self.path / "events.ndjson", stored_event)
        self._write_incremental_artifact_from_event(event)

    def _write_incremental_artifact_from_event(self, event: Dict[str, Any]) -> None:
        event_type = str(event.get("type") or event.get("event") or "")
        payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
        artifact_payload = event.get("__artifact_payload") if isinstance(event.get("__artifact_payload"), dict) else None
        if event_type == "stage_done":
            stage_name = str(event.get("stage") or "")
            dimension_opinion = event.get("dimension_opinion")
            if (
                stage_name in {"technical", "intel", "capital", "fundamental"}
                and isinstance(dimension_opinion, dict)
            ):
                dimensions = _read_trace_json_file(self.path / "dimensions.json", {})
                if not isinstance(dimensions, dict):
                    dimensions = {}
                dimensions[stage_name] = {
                    "stage": stage_name,
                    "status": event.get("status"),
                    "duration": event.get("duration"),
                    "quality": event.get("dimension_status") or {},
                    "opinion": dimension_opinion,
                }
                _write_trace_json(self.path / "dimensions.json", dimensions)
        stage_name_by_event = {
            "selection_candidate_discovery_done": "candidate_discovery",
            "selection_balanced_candidate_evidence_done": "balanced_candidate_evidence",
            "selection_candidate_screening_done": "candidate_screening",
            "selection_deep_dive_done": "single_stock_deep_dive",
            "selection_deterministic_scoring_done": "deterministic_scoring",
            "selection_meta_orchestrator_done": "meta_orchestrator",
            "selection_pricing_agent_done": "pricing_agent",
            "selection_allocation_done": "portfolio_allocation",
            "selection_adversarial_done": "adversarial_review",
            "selection_judge_done": "judge_decision",
        }
        stage_name = stage_name_by_event.get(event_type)
        if stage_name and artifact_payload:
            safe_name = re.sub(r"[^A-Za-z0-9_.-]+", "_", stage_name).strip("._") or "stage"
            _write_trace_json(self.path / f"{safe_name}.json", artifact_payload)
        if event_type == "selection_seed_pool_built":
            _write_trace_json(
                self.path / "seed_pool.json",
                {
                    "event_type": event_type,
                    "phase": payload.get("phase") or "built",
                    "seed_pool_summary": payload.get("seed_pool_summary"),
                    "seed_pool_diagnostics": payload.get("seed_pool_diagnostics"),
                    "seed_pool_hard_exclusion": payload.get("seed_pool_hard_exclusion"),
                    "seed_source_quality": payload.get("seed_source_quality"),
                    "seed_market_regime": payload.get("seed_market_regime"),
                },
            )
        elif event_type == "selection_seed_gate_done":
            _write_trace_json(
                self.path / "seed_gate.json",
                {
                    "event_type": event_type,
                    "phase": payload.get("phase") or "gate",
                    "status": payload.get("status"),
                    "seed_pool_summary_before_gate": payload.get("seed_pool_summary_before_gate"),
                    "seed_pool_summary": payload.get("seed_pool_summary"),
                    "seed_gate": payload.get("seed_gate"),
                    "candidate_count": payload.get("candidate_count"),
                    "candidate_source": payload.get("candidate_source"),
                },
            )
        elif event_type == "selection_seed_facts":
            packets = payload.get("packets") if isinstance(payload.get("packets"), list) else []
            _write_trace_json_compact(
                self.path / "seed_facts.json",
                {
                    "event_type": event_type,
                    "phase": payload.get("phase") or "pre_desk_facts",
                    "total": payload.get("total"),
                    "ok": payload.get("ok"),
                    "partial": payload.get("partial"),
                    "failed": payload.get("failed"),
                    "elapsed_ms": payload.get("elapsed_ms"),
                    "packets_ref": payload.get("packets_ref") or "seed_facts.json",
                    "tool_status_counts": payload.get("tool_status_counts"),
                    "packets_preview": payload.get("packets_preview"),
                    "packets": compact_seed_fact_packets_for_model(packets, limit=len(packets)),
                },
            )

    def finalize(
        self,
        *,
        result: Any = None,
        error: Optional[str] = None,
        context: Optional[Dict[str, Any]] = None,
        request: Optional[AgentTraceRunRequest] = None,
    ) -> None:
        if not self._initialized:
            return
        tool_calls = _normalize_tool_calls_status(
            (getattr(result, "tool_calls_log", []) if result is not None else []) or []
        )
        final_content = getattr(result, "content", "") if result is not None else ""
        payload = {
            "success": bool(getattr(result, "success", False)) if result is not None else False,
            "error": error if error is not None else getattr(result, "error", None),
            "total_steps": getattr(result, "total_steps", 0) if result is not None else 0,
            "total_tokens": getattr(result, "total_tokens", 0) if result is not None else 0,
            "provider": getattr(result, "provider", "") if result is not None else "",
            "model": getattr(result, "model", "") if result is not None else "",
            "artifact_dir": str(self.path),
        }
        if context is not None:
            payload["context_summary"] = _build_trace_context_summary(context)
        _write_trace_json(self.path / "tool_calls.json", tool_calls)
        _write_trace_json(self.path / "evidence_ledger.json", _build_evidence_ledger(tool_calls))
        from src.agent.dimension_evidence import build_dimension_evidence_summary

        dimension_evidence = build_dimension_evidence_summary(tool_calls)
        existing_dimensions = _read_trace_json_file(self.path / "dimensions.json", {})
        if not isinstance(existing_dimensions, dict):
            existing_dimensions = {}
        stored_dimensions: Dict[str, Any] = {}
        for dimension, details in dimension_evidence.items():
            existing = existing_dimensions.get(dimension)
            if isinstance(existing, dict):
                stored_dimensions[dimension] = {
                    **existing,
                    "tool_evidence": details,
                }
                continue
            stored_dimensions[dimension] = {
                "stage": dimension,
                "status": "completed" if details["quality"] != "unavailable" else "degraded",
                "source": "single_agent_tool_evidence",
                "quality": details,
                "opinion": {
                    "quality": details["quality"],
                    "missing_evidence": details["missing_evidence"],
                    "evidence_tools": details["available_tools"] + details["partial_tools"],
                },
            }
        _write_trace_json(
            self.path / "dimensions.json",
            stored_dimensions,
        )
        payload["dimension_evidence"] = dimension_evidence
        debate = getattr(result, "debate", None) if result is not None else None
        if debate is None:
            debate = _build_debate_from_trace_events(self._events)
        _write_trace_json(self.path / "debate.json", debate)
        report_quality = getattr(result, "report_quality", None) if result is not None else None
        _write_trace_json(self.path / "report_quality.json", report_quality)
        payload["report_quality"] = report_quality
        composite_verdict = None
        risk_gate = None
        if context is not None:
            composite_verdict = _build_trace_composite_verdict_payload(
                result=result,
                context=context,
                tool_calls=tool_calls,
            )
            risk_gate = _build_trace_risk_gate_payload(
                result=result,
                context=context,
                tool_calls=tool_calls,
                composite_payload=composite_verdict,
            )
        _write_trace_json(self.path / "composite_verdict.json", composite_verdict)
        _write_trace_json(self.path / "risk_gate.json", risk_gate)
        payload["composite_verdict"] = composite_verdict
        payload["risk_gate"] = risk_gate
        stock_selection = getattr(result, "stock_selection", None) if result is not None else None
        if stock_selection:
            _write_trace_json(self.path / "stock_selection.json", stock_selection)
            selection_context = stock_selection.get("selection_context") or {}
            _write_trace_json(self.path / "selection_context.json", selection_context)
            final_report_json = stock_selection.get("final_report_json") or {}
            _write_trace_json(self.path / "final_report.json", final_report_json)
            for artifact_name, artifact_payload in _extract_evidence_artifacts(final_report_json).items():
                if isinstance(artifact_payload, str):
                    suffix = Path(artifact_name).suffix
                    file_name = artifact_name if suffix else f"{artifact_name}.md"
                    (self.path / file_name).write_text(artifact_payload, encoding="utf-8")
                else:
                    _write_trace_json(self.path / f"{artifact_name}.json", artifact_payload)
            stages = (selection_context.get("stages") or {}) if isinstance(selection_context, dict) else {}
            for stage_name, stage_payload in stages.items():
                if not isinstance(stage_payload, dict):
                    continue
                safe_name = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(stage_name)).strip("._") or "stage"
                _write_trace_json(self.path / f"{safe_name}.json", stage_payload)
        llm_telemetry = _build_llm_telemetry_summary(self.path)
        judge_sanity = _build_judge_sanity_summary(stock_selection)
        _write_trace_json(self.path / "llm_telemetry.json", llm_telemetry)
        _write_trace_json(self.path / "judge_sanity.json", judge_sanity)
        payload["llm_telemetry"] = llm_telemetry
        payload["judge_sanity"] = judge_sanity
        _write_trace_json(self.path / "summary.json", payload)
        final_content = getattr(result, "content", final_content) if result is not None else final_content
        (self.path / "final.md").write_text(final_content or "", encoding="utf-8")
        if context is not None:
            planner = _build_planner_trace(context)
            context_summary = _build_trace_context_summary(context)
            (self.path / "todo.md").write_text(
                _planner_to_todo_md(planner, context_summary, tool_calls),
                encoding="utf-8",
            )
        if request is not None and context is not None:
            _ingest_trace_to_graphiti(
                session_id=self.session_id,
                artifact_dir=str(self.path),
                request=request,
                context=context,
                result=result,
            )


def _extract_evidence_artifacts(final_report_json: Dict[str, Any]) -> Dict[str, Any]:
    """Extract compact multi-expert evidence artifacts from a final report."""
    if not isinstance(final_report_json, dict):
        return {}
    expert_state = final_report_json.get("expert_state") if isinstance(final_report_json.get("expert_state"), dict) else {}
    bundle = expert_state.get("evidence_bundle") if isinstance(expert_state.get("evidence_bundle"), dict) else {}
    artifacts: Dict[str, Any] = {}
    for key in ("evidence_cards", "expert_packets", "judge_input_packet"):
        value = bundle.get(key)
        if value not in (None, [], {}):
            artifacts[key] = value
    balanced_stage = final_report_json.get("balanced_candidate_evidence")
    if isinstance(balanced_stage, dict):
        full = balanced_stage.get("full") if isinstance(balanced_stage.get("full"), dict) else {}
        evidence_json = full.get("candidate_evidence_json")
        if evidence_json not in (None, [], {}):
            artifacts["candidate_evidence"] = evidence_json
        evidence_md = full.get("candidate_evidence_md")
        if isinstance(evidence_md, str) and evidence_md.strip():
            artifacts["candidate_evidence.md"] = evidence_md
    return artifacts


def _jsonable_trace_context(context: Dict[str, Any]) -> Dict[str, Any]:
    payload = dict(context)
    if "agent_user_context" in payload:
        payload["agent_user_context"] = _serialize_agent_user_context(payload.get("agent_user_context"))
    return json.loads(json.dumps(payload, ensure_ascii=False, default=str))


def _ingest_trace_to_graphiti(
    *,
    session_id: str,
    artifact_dir: str,
    request: AgentTraceRunRequest,
    context: Dict[str, Any],
    result: Any,
) -> None:
    config = get_config()
    if not getattr(config, "graphiti_enabled", False):
        return

    try:
        service = get_graphiti_service()
        if not service.is_available():
            logger.debug("Agent trace Graphiti service unavailable, skip ingest")
            return

        title = request.stock_name or context.get("stock_name") or request.message[:80]
        report = getattr(context.get("agent_user_context"), "report", None)
        trace_type = "stock_selection" if getattr(report, "intent", None) == "watchlist_scan" else "single_stock_analysis"
        normalized_tool_calls = _normalize_tool_calls_status(
            getattr(result, "tool_calls_log", []) if result is not None else []
        )
        market = _resolve_trace_market(context)
        timeout_seconds = max(
            10.0,
            min(600.0, float(getattr(get_config(), "graphiti_outbox_job_timeout_seconds", 120) or 120)),
        )
        service.ingest_trace_sync(
            session_id=session_id,
            trace_type=trace_type,
            title=title,
            result={
                "success": getattr(result, "success", False) if result is not None else False,
                "error": getattr(result, "error", None) if result is not None else None,
                "content": getattr(result, "content", "") if result is not None else "",
                "tool_calls": normalized_tool_calls,
                "debate": getattr(result, "debate", None) if result is not None else None,
                "stock_selection": getattr(result, "stock_selection", None) if result is not None else None,
                "composite_verdict": _read_trace_json_file(Path(artifact_dir) / "composite_verdict.json", None),
                "risk_gate": _read_trace_json_file(Path(artifact_dir) / "risk_gate.json", None),
                "artifact_dir": artifact_dir,
            },
            context={
                "stock_code": context.get("stock_code"),
                "stock_name": context.get("stock_name") or request.stock_name,
                "analysis_mode": request.analysis_mode,
                "report_type": context.get("report_type"),
                "report_language": context.get("report_language"),
                "account_id": request.account_id,
                "agent_user_context": _serialize_agent_user_context(context.get("agent_user_context")),
                "context_summary": _build_trace_context_summary(context),
                "dimension_evidence": _read_trace_json_file(Path(artifact_dir) / "dimensions.json", None),
                "planner": _build_planner_trace(context),
            },
            artifact_dir=artifact_dir,
            market=market,
            user_id=str(request.account_id) if request.account_id is not None else None,
            timeout_seconds=timeout_seconds,
        )
    except Exception as exc:
        message = str(exc) or exc.__class__.__name__
        logger.warning("Agent trace Graphiti ingestion failed for %s: %s", session_id, message, exc_info=True)


def _resolve_trace_market(context: Dict[str, Any]) -> str | None:
    payload = _serialize_agent_user_context(context.get("agent_user_context"))
    if not isinstance(payload, dict):
        return context.get("market")
    accounts = payload.get("accounts") or []
    for account in accounts:
        if isinstance(account, dict) and account.get("market"):
            return str(account.get("market"))
    return context.get("market")


def _build_debate_from_trace_events(events: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    debate_events = [
        event for event in events
        if str(event.get("type", "")).startswith("debate_")
    ]
    if not debate_events:
        return None

    intent = next((event.get("intent") for event in debate_events if event.get("intent")), None)
    primary_event = next((event for event in debate_events if event.get("type") == "debate_primary_done"), {})
    opposing_event = next((event for event in debate_events if event.get("type") == "debate_opposing_done"), {})
    judge_event = next((event for event in debate_events if event.get("type") == "debate_judge_done"), {})

    return {
        "mode": "adversarial_debate",
        "intent": intent,
        "primary_thesis": primary_event.get("primary_thesis"),
        "opposing_thesis": opposing_event.get("opposing_thesis"),
        "judge_decision": judge_event.get("judge_decision"),
        "debug_outputs": {
            "primary_thesis_raw": primary_event.get("raw_output", ""),
            "opposing_thesis_raw": opposing_event.get("raw_output", ""),
            "judge_raw": judge_event.get("raw_output", ""),
        },
        "recovered_from_events": True,
    }


def _safe_artifact_initialize(writer: TraceArtifactWriter, *, request: AgentTraceRunRequest, context: Dict[str, Any]) -> None:
    try:
        writer.initialize(request=request, context=context)
    except Exception as exc:
        logger.warning("Agent trace artifact initialize failed: %s", exc)


def _safe_artifact_event(writer: TraceArtifactWriter, event: Dict[str, Any]) -> None:
    try:
        writer.append_event(event)
    except Exception as exc:
        logger.debug("Agent trace artifact event write skipped: %s", exc)


def _safe_artifact_finalize(
    writer: TraceArtifactWriter,
    *,
    result: Any = None,
    error: Optional[str] = None,
    context: Optional[Dict[str, Any]] = None,
    request: Optional[AgentTraceRunRequest] = None,
) -> None:
    try:
        writer.finalize(result=result, error=error, context=context, request=request)
    except Exception as exc:
        logger.warning("Agent trace artifact finalize failed: %s", exc)


def _capture_signal_lifecycle(
    *,
    message: str,
    session_id: str,
    result: Any,
    context: Optional[Dict[str, Any]] = None,
    composite_payload: Optional[Dict[str, Any]] = None,
    risk_gate_payload: Optional[Dict[str, Any]] = None,
) -> None:
    try:
        from src.services.signal_lifecycle_service import SignalLifecycleService

        if risk_gate_payload is None:
            candidate = getattr(result, "deterministic_risk_gate", None)
            if isinstance(candidate, dict):
                risk_gate_payload = candidate

        SignalLifecycleService().capture_agent_result(
            message=message,
            session_id=session_id,
            result=result,
            context=context,
            composite_payload=composite_payload,
            risk_gate_payload=risk_gate_payload,
        )
    except Exception as exc:
        logger.warning("Signal lifecycle capture skipped: %s", exc, exc_info=True)



def _ensure_single_stock_price_plan(
    *,
    message: str,
    session_id: str,
    result: Any,
    context: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    """Generate one reusable plan artifact for non-trace single-stock paths."""
    if result is None or not bool(getattr(result, "success", False)):
        return None
    if isinstance(getattr(result, "stock_selection", None), dict):
        return None
    existing = getattr(result, "trade_plan_result", None)
    if existing is None:
        existing = context.get("pre_debate_price_plan_result")
    trade_plan_result = None
    if isinstance(existing, dict) and isinstance(existing.get("price_plan"), dict):
        try:
            from src.agent.pricing.schemas import TradePlanResult

            trade_plan_result = TradePlanResult.model_validate(existing)
        except Exception:
            pass
    symbol = str(
        context.get("stock_code")
        or (trade_plan_result.price_plan.symbol if trade_plan_result is not None else "")
        or _infer_stock_code_from_message(message)
        or ""
    ).strip()
    normalized = _normalize_trace_symbol(symbol)
    if len(normalized) != 6 or not normalized.isdigit():
        return None
    from src.agent.pricing.runtime import enrich_trade_plan_result, generate_trade_plan
    from src.services.history_loader import get_frozen_target_date

    as_of = context.get("as_of") or get_frozen_target_date() or datetime.now().date()
    if isinstance(as_of, datetime):
        as_of = as_of.date()
    elif not isinstance(as_of, date):
        as_of = datetime.fromisoformat(str(as_of)[:10]).date()
    if trade_plan_result is None:
        trade_plan_result = generate_trade_plan(
            normalized,
            as_of=as_of,
            trace_ref=session_id,
            persist=True,
        )
    decision_context = dict(context)
    composite_payload = decision_context.get("price_plan_composite_payload")
    if not isinstance(composite_payload, dict):
        try:
            composite_payload = score_symbol_composite(
                normalized,
                as_of=as_of,
                quote_basis="latest_trading_day",
                holding=False,
                snapshot_builder=CompositeSnapshotBuilder(fallback_to_network=True),
                market_regime=_trace_market_regime(
                    getattr(result, "tool_calls_log", []) or []
                ),
            )
        except Exception as exc:
            logger.warning(
                "Single-stock position sizing evidence unavailable for %s: %s",
                normalized,
                exc,
            )
            composite_payload = None
    decision_context["price_plan_composite_payload"] = composite_payload
    trade_plan_result = enrich_trade_plan_result(
        trade_plan_result,
        agent_result=result,
        context=decision_context,
    )
    result.price_plan = trade_plan_result.price_plan.model_dump(mode="json")
    result.trade_plan_result = trade_plan_result.model_dump(mode="json")
    from src.agent.pricing.report import merge_price_plan_into_report

    result.content = merge_price_plan_into_report(
        str(getattr(result, "content", "") or ""),
        trade_plan_result,
    )
    gate_context = dict(decision_context)
    if _coerce_trace_agent_user_context(gate_context) is None:
        gate_context["agent_user_context"] = AgentUserContext(
            report=ReportContext(
                intent="entry_analysis",
                primary_symbol=normalized,
                target_symbols=[normalized],
            )
        )
    gate_context["stock_code"] = normalized
    gate_payload = _build_trace_risk_gate_payload(
        result=result,
        context=gate_context,
        tool_calls=getattr(result, "tool_calls_log", []) or [],
        composite_payload=composite_payload,
    )
    if isinstance(gate_payload, dict):
        result.deterministic_risk_gate = gate_payload
    return result.trade_plan_result


def _prepare_signal_lifecycle_context(*, message: str, context: Dict[str, Any]) -> None:
    try:
        from src.services.signal_lifecycle_service import SignalLifecycleService

        service = SignalLifecycleService()
        if service.is_entry_request(message=message, context=context):
            context["signal_lifecycle_entry_intent"] = True
        if service.should_request_plan(message=message, context=context):
            context["signal_lifecycle_plan_candidate"] = True
        active = service.reconcile_for_request(message=message, context=context)
        if active:
            context["active_entry_signals"] = active
    except Exception as exc:
        logger.warning("Signal lifecycle reconciliation skipped: %s", exc, exc_info=True)


@router.post("/trace/run", response_model=AgentTraceRunResponse)
async def run_agent_trace(request: AgentTraceRunRequest, background_tasks: BackgroundTasks = None):
    """Run an Agent request and return developer-oriented plan/execute trace data."""
    config = get_config()
    if not config.is_agent_available():
        raise HTTPException(status_code=400, detail="Agent mode is not enabled")

    requested_session_id = (request.session_id or uuid.uuid4().hex).strip()
    session_id = requested_session_id if requested_session_id.startswith("trace-") else f"trace-{requested_session_id}"
    logger.info(
        "Agent trace run requested: session_id=%s mode=%s message_preview=%s",
        session_id,
        request.analysis_mode,
        request.message[:120],
    )
    skills = request.effective_skills
    context = _build_trace_context(request=request, config=config)
    context["trace_composite_verdict"] = await asyncio.to_thread(_prefetch_trace_composite_verdict, context)
    context["session_id"] = session_id
    context["trace_id"] = session_id
    if skills is not None:
        context["skills"] = skills
    await asyncio.to_thread(
        _prepare_signal_lifecycle_context,
        message=request.message,
        context=context,
    )
    events: List[Dict[str, Any]] = []
    artifact_writer = TraceArtifactWriter(session_id)
    _safe_artifact_initialize(artifact_writer, request=request, context=context)

    def progress_callback(event: dict):
        event_payload = dict(event)
        event_payload = _normalize_tool_event_status(event_payload)
        if event_payload.get("type") in ("tool_start", "tool_done"):
            tool = event_payload.get("tool", "")
            event_payload["display_name"] = TOOL_DISPLAY_NAMES.get(tool, tool)
        _safe_artifact_event(artifact_writer, event_payload)
        event_payload.pop("__artifact_payload", None)
        events.append(event_payload)

    def run_sync():
        executor = _build_executor(config, skills or None)
        with llm_telemetry_scope(trace_id=session_id, artifact_dir=str(artifact_writer.path)):
            result = executor.chat(
                message=request.message,
                session_id=session_id,
                progress_callback=progress_callback,
                context=context,
            )
        _safe_artifact_finalize(artifact_writer, result=result, context=context, request=request)
        return result

    try:
        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(None, run_sync)
    except Exception as exc:
        logger.error("Agent trace run failed: %s", exc, exc_info=True)
        _safe_artifact_finalize(artifact_writer, error=str(exc), context=context, request=request)
        raise HTTPException(status_code=500, detail=str(exc))
    finally:
        try:
            from src.agent.conversation import conversation_manager
            from src.storage import get_db

            conversation_manager.clear(session_id)
            get_db().delete_conversation_session(session_id)
        except Exception as exc:
            logger.debug("Agent trace session cleanup skipped: %s", exc)

    normalized_tool_calls = _normalize_tool_calls_status(result.tool_calls_log)
    composite_verdict = _read_trace_json_file(artifact_writer.path / "composite_verdict.json", None)
    risk_gate = _read_trace_json_file(artifact_writer.path / "risk_gate.json", None)
    await asyncio.to_thread(
        _capture_signal_lifecycle,
        message=request.message,
        session_id=session_id,
        result=result,
        context=context,
        composite_payload=composite_verdict,
        risk_gate_payload=risk_gate,
    )
    llm_telemetry = _read_trace_json_file(artifact_writer.path / "llm_telemetry.json", None) or _build_llm_telemetry_summary(artifact_writer.path)
    judge_sanity = _read_trace_json_file(artifact_writer.path / "judge_sanity.json", None) or _build_judge_sanity_summary(getattr(result, "stock_selection", None))
    dimension_evidence = _read_trace_json_file(artifact_writer.path / "dimensions.json", None)

    return AgentTraceRunResponse(
        success=result.success,
        session_id=session_id,
        content=result.content,
        error=result.error,
        total_steps=result.total_steps,
        total_tokens=result.total_tokens,
        provider=result.provider,
        model=result.model,
        mode=request.analysis_mode,
        events=events,
        tool_calls=normalized_tool_calls,
        planner=_build_planner_trace(context),
        agent_user_context=_serialize_agent_user_context(context.get("agent_user_context")),
        context_summary=_build_trace_context_summary(context),
        dimension_evidence=dimension_evidence,
        report_quality=getattr(result, "report_quality", None),
        debate=result.debate,
        stock_selection=getattr(result, "stock_selection", None),
        composite_verdict=composite_verdict,
        risk_gate=risk_gate,
        price_plan=getattr(result, "price_plan", None),
        trade_plan_result=getattr(result, "trade_plan_result", None),
        llm_telemetry=llm_telemetry,
        judge_sanity=judge_sanity,
        artifact_dir=str(artifact_writer.path),
        runtime_config=_build_agent_runtime_config(config),
    )


@router.post("/trace/stream")
async def stream_agent_trace(request: AgentTraceRunRequest, background_tasks: BackgroundTasks = None):
    """Run an Agent trace request and stream plan/execute events as SSE."""
    config = get_config()
    if not config.is_agent_available():
        raise HTTPException(status_code=400, detail="Agent mode is not enabled")

    requested_session_id = (request.session_id or uuid.uuid4().hex).strip()
    session_id = requested_session_id if requested_session_id.startswith("trace-") else f"trace-{requested_session_id}"
    logger.info(
        "Agent trace stream requested: session_id=%s mode=%s message_preview=%s",
        session_id,
        request.analysis_mode,
        request.message[:120],
    )
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue = asyncio.Queue()
    skills = request.effective_skills
    context = _build_trace_context(request=request, config=config)
    context["trace_composite_verdict"] = await asyncio.to_thread(_prefetch_trace_composite_verdict, context)
    context["session_id"] = session_id
    context["trace_id"] = session_id
    if skills is not None:
        context["skills"] = skills
    await asyncio.to_thread(
        _prepare_signal_lifecycle_context,
        message=request.message,
        context=context,
    )
    artifact_writer = TraceArtifactWriter(session_id)
    _safe_artifact_initialize(artifact_writer, request=request, context=context)

    def put_event(event: Dict[str, Any]) -> None:
        _safe_artifact_event(artifact_writer, event)
        stream_event = dict(event)
        stream_event.pop("__artifact_payload", None)
        asyncio.run_coroutine_threadsafe(queue.put(stream_event), loop)

    def progress_callback(event: dict):
        event_payload = dict(event)
        event_payload = _normalize_tool_event_status(event_payload)
        if event_payload.get("type") in ("tool_start", "tool_done"):
            tool = event_payload.get("tool", "")
            event_payload["display_name"] = TOOL_DISPLAY_NAMES.get(tool, tool)
        put_event(event_payload)

    def cleanup_trace_session() -> None:
        try:
            from src.agent.conversation import conversation_manager
            from src.storage import get_db

            conversation_manager.clear(session_id)
            get_db().delete_conversation_session(session_id)
        except Exception as exc:
            logger.debug("Agent trace stream session cleanup skipped: %s", exc)

    def run_sync():
        try:
            put_event({
                "type": "context_ready",
                "session_id": session_id,
            "context_summary": _build_trace_context_summary(context),
                "dimension_evidence": _read_trace_json_file(artifact_writer.path / "dimensions.json", None),
                "agent_user_context": _serialize_agent_user_context(context.get("agent_user_context")),
                "runtime_config": _build_agent_runtime_config(config),
            })
            put_event({
                "type": "planner_ready",
                "session_id": session_id,
                "planner": _build_planner_trace(context),
                "context_summary": _build_trace_context_summary(context),
                "runtime_config": _build_agent_runtime_config(config),
            })
            executor = _build_executor(config, skills or None)
            with llm_telemetry_scope(trace_id=session_id, artifact_dir=str(artifact_writer.path)):
                result = executor.chat(
                    message=request.message,
                    session_id=session_id,
                    progress_callback=progress_callback,
                    context=context,
                )
            _safe_artifact_finalize(artifact_writer, result=result, context=context, request=request)
            normalized_tool_calls = _normalize_tool_calls_status(result.tool_calls_log)
            composite_verdict = _read_trace_json_file(artifact_writer.path / "composite_verdict.json", None)
            risk_gate = _read_trace_json_file(artifact_writer.path / "risk_gate.json", None)
            _capture_signal_lifecycle(
                message=request.message,
                session_id=session_id,
                result=result,
                context=context,
                composite_payload=composite_verdict,
                risk_gate_payload=risk_gate,
            )
            llm_telemetry = _read_trace_json_file(artifact_writer.path / "llm_telemetry.json", None) or _build_llm_telemetry_summary(artifact_writer.path)
            judge_sanity = _read_trace_json_file(artifact_writer.path / "judge_sanity.json", None) or _build_judge_sanity_summary(getattr(result, "stock_selection", None))
            dimension_evidence = _read_trace_json_file(artifact_writer.path / "dimensions.json", None)
            put_event({
                "type": "done",
                "success": result.success,
                "session_id": session_id,
                "content": result.content,
                "error": result.error,
                "total_steps": result.total_steps,
                "total_tokens": result.total_tokens,
                "provider": result.provider,
                "model": result.model,
                "mode": request.analysis_mode,
                "events": [],
                "tool_calls": normalized_tool_calls,
                "planner": _build_planner_trace(context),
                "agent_user_context": _serialize_agent_user_context(context.get("agent_user_context")),
                "context_summary": _build_trace_context_summary(context),
                "dimension_evidence": dimension_evidence,
                "report_quality": getattr(result, "report_quality", None),
                "debate": result.debate,
                "stock_selection": getattr(result, "stock_selection", None),
                "composite_verdict": composite_verdict,
                "risk_gate": risk_gate,
                "price_plan": getattr(result, "price_plan", None),
                "trade_plan_result": getattr(result, "trade_plan_result", None),
                "llm_telemetry": llm_telemetry,
                "judge_sanity": judge_sanity,
                "artifact_dir": str(artifact_writer.path),
                "runtime_config": _build_agent_runtime_config(config),
            })
        except Exception as exc:
            logger.error("Agent trace stream failed: %s", exc, exc_info=True)
            _safe_artifact_finalize(artifact_writer, error=str(exc), context=context, request=request)
            put_event({"type": "error", "session_id": session_id, "message": str(exc)})
        finally:
            cleanup_trace_session()

    async def event_generator():
        fut = loop.run_in_executor(None, run_sync)
        try:
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=30.0)
                except asyncio.TimeoutError:
                    event = {"type": "heartbeat", "session_id": session_id, "message": "Trace still running"}
                yield "data: " + json.dumps(_sanitize_json_payload(event), ensure_ascii=False, allow_nan=False) + "\n\n"
                if event.get("type") in ("done", "error"):
                    break
        finally:
            try:
                await asyncio.wait_for(fut, timeout=5.0)
            except asyncio.CancelledError:
                pass
            except asyncio.TimeoutError:
                logger.debug("agent trace stream cleanup timed out after 5s for session %s", session_id)
            except Exception as exc:
                logger.warning("agent trace stream cleanup error (ignored): %s", exc, exc_info=True)

    return StreamingResponse(
        event_generator(),
        background=background_tasks,
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


async def _run_research_in_background(
    agent,
    question: str,
    context: Optional[Dict[str, Any]],
    *,
    timeout: int,
):
    """Run deep research off the event loop with an internal overall timeout."""
    return await asyncio.to_thread(
        agent.research,
        question,
        context,
        timeout_seconds=timeout,
    )


# ============================================================
# Deep research endpoint
# ============================================================

class ResearchRequest(BaseModel):
    question: str
    stock_code: Optional[str] = None

class ResearchResponse(BaseModel):
    success: bool
    content: str
    sources: List[str] = Field(default_factory=list)
    token_usage: int = 0
    error: Optional[str] = None


@router.post("/research", response_model=ResearchResponse)
async def agent_research(request: ResearchRequest):
    """Run a deep-research query via the ResearchAgent.

    Similar to the ``/research`` bot command but exposed as a REST endpoint.
    """
    config = get_config()
    if not config.is_agent_available():
        raise HTTPException(status_code=400, detail="Agent mode is not enabled")

    question = request.question
    context: Optional[Dict[str, Any]] = None
    if request.stock_code:
        question = f"[Stock: {request.stock_code}] {question}"
        context = {"stock_code": request.stock_code}

    try:
        from src.agent.research import ResearchAgent
        from src.agent.factory import get_tool_registry
        from src.agent.llm_adapter import LLMToolAdapter

        registry = get_tool_registry()
        llm_adapter = LLMToolAdapter(config)
        budget = getattr(config, "agent_deep_research_budget", 30000)

        agent = ResearchAgent(
            tool_registry=registry,
            llm_adapter=llm_adapter,
            token_budget=budget,
        )

        research_timeout = getattr(config, "agent_deep_research_timeout", 180)

        result = await _run_research_in_background(
            agent,
            question,
            context,
            timeout=research_timeout,
        )
        if getattr(result, "timed_out", False):
            logger.warning("Agent research API timed out after %ss", research_timeout)
            return ResearchResponse(
                success=False,
                content="",
                sources=[],
                token_usage=0,
                error=f"Deep research timed out after {research_timeout}s",
            )

        return ResearchResponse(
            success=result.success,
            content=result.report,
            sources=[f"Sub-question {i+1}: {q}" for i, q in enumerate(result.sub_questions)],
            token_usage=result.total_tokens,
            error=result.error if not result.success else None,
        )
    except Exception as e:
        logger.error("Agent research API failed: %s", e)
        logger.exception("Agent research error details:")
        raise HTTPException(status_code=500, detail=str(e))
