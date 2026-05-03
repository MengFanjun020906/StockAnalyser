# -*- coding: utf-8 -*-
"""
Agent API endpoints.
"""

import asyncio
import json
import logging
import uuid
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import AliasChoices, BaseModel, ConfigDict, Field

from src.config import get_config
from src.services.agent_model_service import list_agent_model_deployments

# Tool name -> Chinese display name mapping
TOOL_DISPLAY_NAMES: Dict[str, str] = {
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
    risk_preference: Optional[str] = None
    trading_horizon: Optional[str] = None
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
                notes=request.investor_notes,
            )
        except Exception as exc:
            logger.warning("Agent trace portfolio context injection failed: %s", exc)
            context["_trace_context_error"] = str(exc)

    return context


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

    def progress_callback(event: dict):
        event_payload = dict(event)
        if event_payload.get("type") in ("tool_start", "tool_done"):
            tool = event_payload.get("tool", "")
            event_payload["display_name"] = TOOL_DISPLAY_NAMES.get(tool, tool)
        events.append(event_payload)

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
        raise HTTPException(status_code=500, detail=str(exc))
    finally:
        try:
            from src.agent.conversation import conversation_manager
            from src.storage import get_db

            conversation_manager.clear(session_id)
            get_db().delete_conversation_session(session_id)
        except Exception as exc:
            logger.debug("Agent trace session cleanup skipped: %s", exc)

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
        tool_calls=result.tool_calls_log,
        planner=_build_planner_trace(context),
        agent_user_context=_serialize_agent_user_context(context.get("agent_user_context")),
        context_summary=_build_trace_context_summary(context),
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

    def put_event(event: Dict[str, Any]) -> None:
        asyncio.run_coroutine_threadsafe(queue.put(event), loop)

    def progress_callback(event: dict):
        event_payload = dict(event)
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
                "tool_calls": result.tool_calls_log,
                "planner": _build_planner_trace(context),
                "agent_user_context": _serialize_agent_user_context(context.get("agent_user_context")),
                "context_summary": _build_trace_context_summary(context),
            })
        except Exception as exc:
            logger.error("Agent trace stream failed: %s", exc, exc_info=True)
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
