# -*- coding: utf-8 -*-
"""
Agent API endpoints.
"""

import asyncio
import json
import logging
import re
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import AliasChoices, BaseModel, ConfigDict, Field

from src.config import get_config
from src.schemas.agent_context import AgentUserContext, ReportContext, ReportIntent
from src.schemas.agent_signal import TradeAction, TradePlan
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
    "get_stock_info":             "获取股票基本面",
    "search_stock_news":          "搜索股票新闻",
    "search_comprehensive_intel": "搜索综合情报",
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

class ChatRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    message: str
    session_id: Optional[str] = None
    skills: Optional[List[str]] = Field(
        default=None,
        validation_alias=AliasChoices("skills", "strategies"),
    )
    context: Optional[Dict[str, Any]] = None  # Previous analysis context for data reuse

    @property
    def effective_skills(self) -> Optional[List[str]]:
        """Return skill ids from the unified request shape."""
        return self.skills

class ChatResponse(BaseModel):
    success: bool
    content: str
    session_id: str
    error: Optional[str] = None


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
    debate: Optional[Dict[str, Any]] = None
    stock_selection: Optional[Dict[str, Any]] = None
    risk_gate: Optional[Dict[str, Any]] = None
    artifact_dir: Optional[str] = None

class SkillInfo(BaseModel):
    id: str
    name: str
    description: str

class SkillsResponse(BaseModel):
    skills: List[SkillInfo]
    default_skill_id: str = ""


class StrategiesResponse(BaseModel):
    strategies: List[SkillInfo]
    default_strategy_id: str = ""


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


@router.get("/models", response_model=AgentModelsResponse)
async def get_agent_models():
    """Get configured Agent model deployments for frontend selection."""
    config = get_config()
    return AgentModelsResponse(
        models=[AgentModelDeployment(**item) for item in list_agent_model_deployments(config)]
    )


def _build_skills_response(config) -> SkillsResponse:
    from src.agent.factory import get_skill_manager
    from src.agent.skills.defaults import get_primary_default_skill_id

    skill_manager = get_skill_manager(config)
    available_skills = sorted(
        [
            skill
            for skill in skill_manager.list_skills()
            if getattr(skill, "user_invocable", True)
        ],
        key=lambda skill: (
            int(getattr(skill, "default_priority", 100)),
            skill.display_name,
            skill.name,
        ),
    )
    skills = [
        SkillInfo(id=skill.name, name=skill.display_name, description=skill.description)
        for skill in available_skills
    ]
    return SkillsResponse(
        skills=skills,
        default_skill_id=get_primary_default_skill_id(available_skills),
    )


@router.get("/skills", response_model=SkillsResponse)
async def get_skills():
    """
    Get available agent strategy skills.
    """
    return _build_skills_response(get_config())


@router.get("/strategies", response_model=StrategiesResponse, include_in_schema=False)
async def get_strategies():
    """Compatibility alias for legacy clients."""
    payload = _build_skills_response(get_config())
    return StrategiesResponse(
        strategies=payload.skills,
        default_strategy_id=payload.default_skill_id,
    )

@router.post("/chat", response_model=ChatResponse)
async def agent_chat(request: ChatRequest):
    """
    Chat with the AI Agent.
    """
    config = get_config()
    
    if not config.is_agent_available():
        raise HTTPException(status_code=400, detail="Agent mode is not enabled")
        
    session_id = request.session_id or str(uuid.uuid4())
    
    try:
        skills = request.effective_skills
        executor = _build_executor(config, skills or None)

        # Pass explicit skills into context for the orchestrator.
        # Direct assignment so caller-provided skills always take precedence
        # over any stale value carried in the context dict.
        ctx = dict(request.context or {})
        if skills is not None:
            ctx["skills"] = skills

        # Offload the blocking call to a thread to avoid blocking the event loop.
        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(
            None,
            lambda: executor.chat(message=request.message, session_id=session_id,
                                  context=ctx),
        )

        return ChatResponse(
            success=result.success,
            content=result.content,
            session_id=session_id,
            error=result.error
        )
            
    except Exception as e:
        logger.error(f"Agent chat API failed: {e}")
        logger.exception("Agent chat error details:")
        raise HTTPException(status_code=500, detail=str(e))


class SessionItem(BaseModel):
    session_id: str
    title: str
    message_count: int
    created_at: Optional[str] = None
    last_active: Optional[str] = None

class SessionsResponse(BaseModel):
    sessions: List[SessionItem]

class SessionMessagesResponse(BaseModel):
    session_id: str
    messages: List[Dict[str, Any]]


@router.get("/chat/sessions", response_model=SessionsResponse)
async def list_chat_sessions(limit: int = 50, user_id: Optional[str] = None):
    """获取聊天会话列表

    Args:
        limit: Maximum number of sessions to return.
        user_id: Optional platform-prefixed user identifier for session
            isolation.  When provided, only sessions whose session_id
            starts with this prefix are returned.  The value must
            include the platform prefix, e.g. ``telegram_12345``,
            ``feishu_ou_abc``.
    """
    from src.storage import get_db
    sessions = get_db().get_chat_sessions(
        limit=limit,
        session_prefix=user_id,
        extra_session_ids=[user_id] if user_id else None,
    )
    return SessionsResponse(sessions=sessions)


@router.get("/chat/sessions/{session_id}", response_model=SessionMessagesResponse)
async def get_chat_session_messages(session_id: str, limit: int = 100):
    """获取单个会话的完整消息"""
    from src.storage import get_db
    messages = get_db().get_conversation_messages(session_id, limit=limit)
    return SessionMessagesResponse(session_id=session_id, messages=messages)


@router.delete("/chat/sessions/{session_id}")
async def delete_chat_session(session_id: str):
    """删除指定会话"""
    from src.storage import get_db
    count = get_db().delete_conversation_session(session_id)
    return {"deleted": count}


class SendChatRequest(BaseModel):
    """Request body for sending chat content to notification channels."""

    content: str = Field(..., min_length=1, max_length=50000)
    title: Optional[str] = None


@router.post("/chat/send")
async def send_chat_to_notification(request: SendChatRequest):
    """
    Send chat session content to configured notification channels.
    Uses run_in_executor to avoid blocking the event loop.
    """
    from src.notification import NotificationService

    loop = asyncio.get_running_loop()
    success = await loop.run_in_executor(
        None,
        lambda: NotificationService().send(request.content),
    )
    if not success:
        return {
            "success": False,
            "error": "no_channels",
            "message": "未配置通知渠道，请先在设置中配置",
        }
    return {"success": True}


def _build_executor(config, skills: Optional[List[str]] = None):
    """Build and return a configured AgentExecutor (sync helper)."""
    from src.agent.factory import build_agent_executor
    return build_agent_executor(config, skills=skills)


def _infer_stock_code_from_message(message: str) -> str:
    """Best-effort stock code extraction for developer trace convenience."""
    import re

    patterns = [
        r"\b(?:SH|SZ|BJ)?(\d{6})(?:\.(?:SH|SZ|BJ|SS))?\b",
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
    stock_code = (request.stock_code or context.get("stock_code") or _infer_stock_code_from_message(request.message) or "").strip()
    if stock_code:
        context["stock_code"] = stock_code
    if request.stock_name:
        context["stock_name"] = request.stock_name
    context.setdefault("report_type", "detailed")
    context.setdefault("report_language", getattr(config, "report_language", "zh"))

    should_inject = request.inject_portfolio_context and request.analysis_mode == "planning_execute"
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
            _apply_trace_report_overrides(
                context["agent_user_context"],
                report_intent=request.report_intent,
            )
        except Exception as exc:
            logger.warning("Agent trace portfolio context injection failed: %s", exc)
            context["_trace_context_error"] = str(exc)

    if request.analysis_mode == "planning_execute" and "agent_user_context" not in context:
        context["agent_user_context"] = _build_minimal_trace_agent_user_context(
            request=request,
            stock_code=stock_code,
            language=context.get("report_language", "zh"),
        )

    return context


def _build_minimal_trace_agent_user_context(
    *,
    request: AgentTraceRunRequest,
    stock_code: str,
    language: str,
) -> AgentUserContext:
    intent = _resolve_trace_report_intent(request=request, stock_code=stock_code)
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


def _resolve_trace_report_intent(*, request: AgentTraceRunRequest, stock_code: str) -> ReportIntent:
    if request.report_intent and request.report_intent != "auto":
        if not stock_code and _looks_like_stock_selection_request(request.message):
            return "watchlist_scan"
        return request.report_intent
    if _looks_like_stock_selection_request(request.message):
        return "watchlist_scan"
    return "entry_analysis" if stock_code else "qa"


def _looks_like_stock_selection_request(message: str) -> bool:
    import re

    return bool(re.search(r"(选股|筛选|推荐.*股|股票池|组合|配置|分配仓位|仓位分配|买什么|挑.*股)", message))


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
        return
    try:
        setattr(report, "intent", report_intent)
    except Exception:
        logger.debug("Agent trace report intent override skipped")


def _build_trace_context_summary(context: Dict[str, Any]) -> Dict[str, Any]:
    payload = _serialize_agent_user_context(context.get("agent_user_context"))
    summary: Dict[str, Any] = {
        "context_error": context.get("_trace_context_error"),
        "stock_code": context.get("stock_code"),
        "account_count": 0,
        "position_count": 0,
        "target_position": None,
        "accounts": [],
        "investor": None,
        "metadata": {},
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


def _write_trace_json(path: Path, payload: Any) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )


def _append_trace_event(path: Path, event: Dict[str, Any]) -> None:
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(event, ensure_ascii=False, default=str))
        fh.write("\n")


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

    lines.extend(["", "## 维度计划"])
    for capability in planner.get("capabilities") or []:
        lines.append(f"- [x] capability={capability}")
    if not planner.get("capabilities"):
        lines.append("- [ ] 无能力域")

    lines.extend(["", "## 工具计划"])
    if tool_plan:
        for item in tool_plan:
            if not isinstance(item, dict):
                continue
            tools = ", ".join(str(tool) for tool in item.get("tools") or []) or "-"
            missing = ", ".join(str(tool) for tool in item.get("missing_tools") or []) or "-"
            purpose = item.get("purpose") or "-"
            lines.append(f"- [ ] capability={item.get('capability') or '-'} -> tools=[{tools}] -> purpose={purpose}")
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

    _append_execute_status_to_todo(lines, tool_plan, tool_calls or [])

    return "\n".join(lines) + "\n"


def _append_execute_status_to_todo(
    lines: List[str],
    tool_plan: Any,
    tool_calls: List[Dict[str, Any]],
) -> None:
    executed_tools = _summarize_executed_tools(tool_calls)
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
            preview = call.get("result_preview")
            if preview:
                lines.append(f"  result_preview={preview}")
    else:
        lines.append("- [ ] 尚未执行工具")

    planned_tools = _planned_tool_names(tool_plan)
    not_called = [tool for tool in planned_tools if tool not in executed_tools]
    if not_called:
        lines.extend(["", "## 未调用计划工具", *[f"- [ ] {tool}" for tool in not_called]])

    lines.extend(["", "## Execute Protocol 复核"])
    if tool_calls:
        failed = [call.get("tool") for call in tool_calls if isinstance(call, dict) and _tool_call_status(call) != "success"]
        lines.append(f"- [{'x' if not failed else ' '}] 工具失败已记录: {', '.join(str(item) for item in failed) if failed else '无'}")
        lines.append("- [x] Evidence Ledger 已进入最终报告复核")
        lines.append("- [x] 停止条件已复核")
    else:
        lines.append("- [ ] 等待工具执行后复核")


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
        return any(str(item).strip() for item in errors)
    return bool(errors)


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
) -> Optional[Dict[str, Any]]:
    """Build and evaluate the deterministic risk gate payload for a trace run."""
    agent_user_context = _coerce_trace_agent_user_context(context)
    if agent_user_context is None:
        return None

    primary_symbol = _resolve_trace_primary_symbol(context, agent_user_context)
    if not primary_symbol:
        return None

    trade_plan = _build_trace_trade_plan(
        result=result,
        context=context,
        agent_user_context=agent_user_context,
        primary_symbol=primary_symbol,
    )
    if trade_plan is None:
        return None

    try:
        from src.agent.risk_gate import QuoteState, RiskGateEvaluator, RiskGateInput

        position = _find_trace_position(agent_user_context, primary_symbol)
        account = _find_trace_account(agent_user_context, position)
        quote = _build_trace_quote_state(primary_symbol, tool_calls, fallback_position=position)
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
        )
        gate_result = RiskGateEvaluator().evaluate(gate_input)
        return {
            "schema_version": 1,
            "trade_plan": trade_plan.model_dump(mode="json"),
            "risk_gate": gate_result.model_dump(mode="json"),
            "quote": quote.__dict__ if quote is not None else None,
            "source": _trace_trade_plan_source(result),
        }
    except Exception as exc:
        logger.warning("Agent trace risk gate build failed: %s", exc, exc_info=True)
        return {
            "schema_version": 1,
            "error": str(exc),
            "trade_plan": trade_plan.model_dump(mode="json"),
        }


def _coerce_trace_agent_user_context(context: Dict[str, Any]) -> Optional[AgentUserContext]:
    try:
        from src.agent.executor import _coerce_agent_user_context

        return _coerce_agent_user_context(context.get("agent_user_context"))
    except Exception as exc:
        logger.debug("Agent trace risk gate context coercion failed: %s", exc)
        return None


def _resolve_trace_primary_symbol(context: Dict[str, Any], agent_user_context: AgentUserContext) -> Optional[str]:
    report = agent_user_context.report
    candidates = [
        context.get("stock_code"),
        report.primary_symbol,
        *(report.target_symbols or []),
        *((position.symbol for position in agent_user_context.positions if position.quantity > 0)),
    ]
    for symbol in candidates:
        normalized = str(symbol or "").strip()
        if normalized:
            return normalized
    return None


def _build_trace_trade_plan(
    *,
    result: Any,
    context: Dict[str, Any],
    agent_user_context: AgentUserContext,
    primary_symbol: str,
) -> Optional[TradePlan]:
    action = _extract_trace_action(result)
    if action is None:
        return None
    position = _find_trace_position(agent_user_context, primary_symbol)
    target_position_pct = _extract_trace_target_position_pct(result, position)
    stop_loss_price = _extract_trace_stop_loss_price(result, position)
    stop_loss_pct = _extract_trace_stop_loss_pct(result, agent_user_context)
    invalidation_conditions = _extract_trace_invalidation_conditions(result)
    if action in {"open", "add", "reduce", "sell"} and not invalidation_conditions:
        invalidation_conditions = _fallback_invalidation_conditions(action, agent_user_context)

    return TradePlan(
        symbol=primary_symbol,
        action=action,
        order_type="manual",
        target_position_pct=target_position_pct,
        stop_loss_price=stop_loss_price,
        stop_loss_pct=stop_loss_pct,
        invalidation_conditions=invalidation_conditions,
        review_triggers=_extract_trace_review_triggers(result),
        notes="Generated from agent trace output for deterministic risk_gate evaluation.",
        metadata={
            "analysis_mode": agent_user_context.report.analysis_mode,
            "intent": agent_user_context.report.intent,
            "stock_name": context.get("stock_name"),
        },
    )


def _extract_trace_action(result: Any) -> Optional[TradeAction]:
    debate = getattr(result, "debate", None) if result is not None else None
    candidates: List[Any] = []
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
    }
    text = mapping.get(text, text)
    allowed = {"open", "add", "reduce", "sell", "hold", "wait", "monitor", "manual_review", "reject"}
    return text if text in allowed else None  # type: ignore[return-value]


def _extract_trace_target_position_pct(result: Any, position: Any) -> Optional[float]:
    debate = getattr(result, "debate", None) if result is not None else None
    if isinstance(debate, dict):
        judge = debate.get("judge_decision") or {}
        for key in ("target_position_pct", "position_pct", "first_position_pct"):
            value = _to_float(judge.get(key))
            if value is not None:
                return value
    return getattr(position, "position_pct", None) if position is not None else None


def _extract_trace_stop_loss_price(result: Any, position: Any) -> Optional[float]:
    debate = getattr(result, "debate", None) if result is not None else None
    if isinstance(debate, dict):
        judge = debate.get("judge_decision") or {}
        value = _to_float(judge.get("stop_loss_price") or judge.get("stop_loss"))
        if value is not None:
            return value
    return getattr(position, "stop_loss", None) if position is not None else None


def _extract_trace_stop_loss_pct(result: Any, agent_user_context: AgentUserContext) -> Optional[float]:
    debate = getattr(result, "debate", None) if result is not None else None
    if isinstance(debate, dict):
        judge = debate.get("judge_decision") or {}
        value = _to_float(judge.get("stop_loss_pct"))
        if value is not None:
            return value
    return agent_user_context.investor.default_stop_loss_pct


def _extract_trace_invalidation_conditions(result: Any) -> List[str]:
    values: List[Any] = []
    debate = getattr(result, "debate", None) if result is not None else None
    if isinstance(debate, dict):
        judge = debate.get("judge_decision") or {}
        values.extend([
            judge.get("invalidation_conditions"),
            judge.get("failure_conditions"),
            judge.get("risk_controls"),
        ])
    return _string_list(values)


def _extract_trace_review_triggers(result: Any) -> List[str]:
    debate = getattr(result, "debate", None) if result is not None else None
    if not isinstance(debate, dict):
        return []
    judge = debate.get("judge_decision") or {}
    return _string_list([judge.get("review_triggers"), judge.get("next_review"), judge.get("risk_controls")])


def _fallback_invalidation_conditions(action: TradeAction, agent_user_context: AgentUserContext) -> List[str]:
    if action in {"open", "add"} and agent_user_context.investor.default_stop_loss_pct is not None:
        return [f"跌破默认止损 {agent_user_context.investor.default_stop_loss_pct:.2f}%"]
    if action in {"reduce", "sell"}:
        return ["卖出/减仓计划需结合可执行交易状态复核"]
    return []


def _trace_trade_plan_source(result: Any) -> str:
    debate = getattr(result, "debate", None) if result is not None else None
    if isinstance(debate, dict) and debate.get("judge_decision"):
        return "debate_judge"
    if isinstance(getattr(result, "stock_selection", None), dict):
        return "stock_selection"
    return "fallback_wait"


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

        quote_payload = _latest_tool_payload(tool_calls, "get_realtime_quote") or {}
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


def _latest_tool_payload(tool_calls: List[Dict[str, Any]], tool_name: str) -> Optional[Dict[str, Any]]:
    for call in reversed(tool_calls or []):
        if not isinstance(call, dict) or call.get("tool") != tool_name:
            continue
        payload = _parse_result_preview(call.get("result_preview"))
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


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "y", "是", "涨停", "跌停"}


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
        self._events.append(event)
        _append_trace_event(self.path / "events.ndjson", event)

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
        debate = getattr(result, "debate", None) if result is not None else None
        if debate is None:
            debate = _build_debate_from_trace_events(self._events)
        _write_trace_json(self.path / "debate.json", debate)
        risk_gate = None
        if context is not None:
            risk_gate = _build_trace_risk_gate_payload(
                result=result,
                context=context,
                tool_calls=tool_calls,
            )
        _write_trace_json(self.path / "risk_gate.json", risk_gate)
        payload["risk_gate"] = risk_gate
        stock_selection = getattr(result, "stock_selection", None) if result is not None else None
        if stock_selection:
            _write_trace_json(self.path / "stock_selection.json", stock_selection)
            selection_context = stock_selection.get("selection_context") or {}
            _write_trace_json(self.path / "selection_context.json", selection_context)
            final_report_json = stock_selection.get("final_report_json") or {}
            _write_trace_json(self.path / "final_report.json", final_report_json)
            stages = (selection_context.get("stages") or {}) if isinstance(selection_context, dict) else {}
            for stage_name, stage_payload in stages.items():
                if not isinstance(stage_payload, dict):
                    continue
                safe_name = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(stage_name)).strip("._") or "stage"
                _write_trace_json(self.path / f"{safe_name}.json", stage_payload)
        _write_trace_json(self.path / "summary.json", payload)
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
        trace_type = (
            "stock_selection"
            if _looks_like_stock_selection_request(request.message)
            else "single_stock_analysis"
        )
        normalized_tool_calls = _normalize_tool_calls_status(
            getattr(result, "tool_calls_log", []) if result is not None else []
        )
        market = _resolve_trace_market(context)
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
                "risk_gate": _build_trace_risk_gate_payload(
                    result=result,
                    context=context,
                    tool_calls=normalized_tool_calls,
                ),
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
                "planner": _build_planner_trace(context),
            },
            artifact_dir=artifact_dir,
            market=market,
            user_id=str(request.account_id) if request.account_id is not None else None,
        )
    except Exception as exc:
        logger.warning("Agent trace Graphiti ingestion failed for %s: %s", session_id, exc, exc_info=True)


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


@router.post("/trace/run", response_model=AgentTraceRunResponse)
async def run_agent_trace(request: AgentTraceRunRequest):
    """Run an Agent request and return developer-oriented plan/execute trace data."""
    config = get_config()
    if not config.is_agent_available():
        raise HTTPException(status_code=400, detail="Agent mode is not enabled")

    requested_session_id = (request.session_id or uuid.uuid4().hex).strip()
    session_id = requested_session_id if requested_session_id.startswith("trace-") else f"trace-{requested_session_id}"
    skills = request.effective_skills
    context = _build_trace_context(request=request, config=config)
    if skills is not None:
        context["skills"] = skills
    events: List[Dict[str, Any]] = []
    artifact_writer = TraceArtifactWriter(session_id)
    _safe_artifact_initialize(artifact_writer, request=request, context=context)

    def progress_callback(event: dict):
        event_payload = dict(event)
        event_payload = _normalize_tool_event_status(event_payload)
        if event_payload.get("type") in ("tool_start", "tool_done"):
            tool = event_payload.get("tool", "")
            event_payload["display_name"] = TOOL_DISPLAY_NAMES.get(tool, tool)
        events.append(event_payload)
        _safe_artifact_event(artifact_writer, event_payload)

    def run_sync():
        executor = _build_executor(config, skills or None)
        return executor.chat(
            message=request.message,
            session_id=session_id,
            progress_callback=progress_callback,
            context=context,
        )

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

    _safe_artifact_finalize(artifact_writer, result=result, context=context, request=request)
    normalized_tool_calls = _normalize_tool_calls_status(result.tool_calls_log)
    risk_gate = _build_trace_risk_gate_payload(
        result=result,
        context=context,
        tool_calls=normalized_tool_calls,
    )

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
        debate=result.debate,
        stock_selection=getattr(result, "stock_selection", None),
        risk_gate=risk_gate,
        artifact_dir=str(artifact_writer.path),
    )


@router.post("/trace/stream")
async def stream_agent_trace(request: AgentTraceRunRequest):
    """Run an Agent trace request and stream plan/execute events as SSE."""
    config = get_config()
    if not config.is_agent_available():
        raise HTTPException(status_code=400, detail="Agent mode is not enabled")

    requested_session_id = (request.session_id or uuid.uuid4().hex).strip()
    session_id = requested_session_id if requested_session_id.startswith("trace-") else f"trace-{requested_session_id}"
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue = asyncio.Queue()
    skills = request.effective_skills
    context = _build_trace_context(request=request, config=config)
    if skills is not None:
        context["skills"] = skills
    artifact_writer = TraceArtifactWriter(session_id)
    _safe_artifact_initialize(artifact_writer, request=request, context=context)

    def put_event(event: Dict[str, Any]) -> None:
        _safe_artifact_event(artifact_writer, event)
        asyncio.run_coroutine_threadsafe(queue.put(event), loop)

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
                "agent_user_context": _serialize_agent_user_context(context.get("agent_user_context")),
            })
            put_event({
                "type": "planner_ready",
                "session_id": session_id,
                "planner": _build_planner_trace(context),
            })
            executor = _build_executor(config, skills or None)
            result = executor.chat(
                message=request.message,
                session_id=session_id,
                progress_callback=progress_callback,
                context=context,
            )
            _safe_artifact_finalize(artifact_writer, result=result, context=context, request=request)
            normalized_tool_calls = _normalize_tool_calls_status(result.tool_calls_log)
            risk_gate = _build_trace_risk_gate_payload(
                result=result,
                context=context,
                tool_calls=normalized_tool_calls,
            )
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
                "debate": result.debate,
                "stock_selection": getattr(result, "stock_selection", None),
                "risk_gate": risk_gate,
                "artifact_dir": str(artifact_writer.path),
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
                    event = await asyncio.wait_for(queue.get(), timeout=300.0)
                except asyncio.TimeoutError:
                    event = {"type": "error", "session_id": session_id, "message": "Trace 分析超时"}
                yield "data: " + json.dumps(event, ensure_ascii=False) + "\n\n"
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


@router.post("/chat/stream")
async def agent_chat_stream(request: ChatRequest):
    """
    Chat with the AI Agent, streaming progress via SSE.
    Each SSE event is a JSON object with a 'type' field:
      - thinking: AI is deciding next action
      - tool_start: a tool call has begun
      - tool_done: a tool call finished
      - generating: final answer being generated
      - done: analysis complete, contains 'content' and 'success'
      - error: error occurred, contains 'message'
    """
    config = get_config()
    if not config.is_agent_available():
        raise HTTPException(status_code=400, detail="Agent mode is not enabled")

    session_id = request.session_id or str(uuid.uuid4())
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue = asyncio.Queue()

    # Pass explicit skills into context for the orchestrator.
    # Direct assignment so caller-provided skills always take precedence.
    skills = request.effective_skills
    stream_ctx = dict(request.context or {})
    if skills is not None:
        stream_ctx["skills"] = skills

    def progress_callback(event: dict):
        event = _normalize_tool_event_status(dict(event))
        # Enrich tool events with display names
        if event.get("type") in ("tool_start", "tool_done"):
            tool = event.get("tool", "")
            event["display_name"] = TOOL_DISPLAY_NAMES.get(tool, tool)
        asyncio.run_coroutine_threadsafe(queue.put(event), loop)

    def run_sync():
        try:
            executor = _build_executor(config, skills or None)
            result = executor.chat(
                message=request.message,
                session_id=session_id,
                progress_callback=progress_callback,
                context=stream_ctx,
            )
            asyncio.run_coroutine_threadsafe(
                queue.put({
                    "type": "done",
                    "success": result.success,
                    "content": result.content,
                    "error": result.error,
                    "total_steps": result.total_steps,
                    "total_tokens": result.total_tokens,
                    "provider": result.provider,
                    "model": result.model,
                    "tool_calls": result.tool_calls_log,
                    "session_id": session_id,
                }),
                loop,
            )
        except Exception as exc:
            logger.error(f"Agent stream error: {exc}")
            asyncio.run_coroutine_threadsafe(
                queue.put({"type": "error", "message": str(exc)}),
                loop,
            )

    async def event_generator():
        # Start executor in a thread so we don't block the event loop
        fut = loop.run_in_executor(None, run_sync)
        try:
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=300.0)
                except asyncio.TimeoutError:
                    yield "data: " + json.dumps({"type": "error", "message": "分析超时"}, ensure_ascii=False) + "\n\n"
                    break
                yield "data: " + json.dumps(event, ensure_ascii=False) + "\n\n"
                if event.get("type") in ("done", "error"):
                    break
        finally:
            try:
                await asyncio.wait_for(fut, timeout=5.0)
            except asyncio.CancelledError:
                pass
            except asyncio.TimeoutError:
                # Cleanup taking longer than 5s is treated as an expected timeout; no warning.
                logger.debug("agent executor cleanup timed out after 5s for session %s", session_id)
            except Exception as exc:
                logger.warning("agent executor cleanup error (ignored): %s", exc, exc_info=True)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )
