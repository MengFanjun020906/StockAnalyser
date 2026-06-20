import json
import sys
import time
from datetime import datetime
from unittest.mock import MagicMock, patch

import pytest

try:
    import litellm  # noqa: F401
except ModuleNotFoundError:
    sys.modules["litellm"] = MagicMock()
try:
    import json_repair  # noqa: F401
except ModuleNotFoundError:
    json_repair_stub = MagicMock()
    json_repair_stub.repair_json.side_effect = lambda content, **_kwargs: content
    sys.modules["json_repair"] = json_repair_stub

from src.agent.executor import AgentExecutor
from src.agent.llm_adapter import LLMResponse
from src.agent.orchestrator import AgentOrchestrator
from src.agent import stock_selection as stock_selection_module
from src.agent.stock_selection import (
    SelectionRunContext,
    _candidate_has_desk_tag,
    _compact_candidate_seed,
    _compact_tool_result_for_trace,
    _normalize_deep_dive_payload,
    _persist_seed_pool_quality_snapshot,
    _resolve_candidate_discovery_mode,
    _run_candidate_discovery_tool,
    _select_deep_dive_targets,
    render_stock_selection_markdown,
    run_stock_selection_pipeline,
    should_run_stock_selection,
)
from src.agent.multi_expert import AgentState, EvidenceBundle, ExpertOpinion
from src.agent.tools.registry import ToolDefinition, ToolParameter, ToolRegistry
from src.schemas.agent_context import AccountContext, AgentUserContext, InvestorProfile, PositionContext, ReportContext


def _registry():
    registry = ToolRegistry()
    registry.register(ToolDefinition(
        name="discover_watchlist_candidates",
        description="discover",
        parameters=[],
        handler=lambda market="cn", seed_symbols=None, limit=8: {
            "status": "ok",
            "market": market,
            "candidates": [
                {"code": "600001", "name": "测试一", "market": "cn", "source": "test"},
            ][:limit],
        },
    ))
    registry.register(ToolDefinition(
        name="detect_market_regime",
        description="regime",
        parameters=[],
        handler=lambda market="cn", persist=True: {
            "status": "ok",
            "market": market,
            "regime": "trending_up",
            "risk_level": "medium",
            "volatility_bucket": "normal",
            "sentiment_state": "neutral",
            "wyckoff_phase": "markup",
            "risk_multiplier": 1.0,
            "strategy_hints": ["趋势向上时可接受回踩确认后的顺势策略。"],
            "data_quality": "limited",
        },
    ))
    registry.register(ToolDefinition(
        name="get_market_indices",
        description="indices",
        parameters=[],
        handler=lambda region="cn": {"region": region, "indices": []},
    ))
    registry.register(ToolDefinition(
        name="get_sector_rankings",
        description="sectors",
        parameters=[],
        handler=lambda top_n=10: {"top_sectors": [{"name": "测试板块", "change_pct": 5.0}]},
    ))
    registry.register(ToolDefinition(
        name="get_realtime_quote",
        description="quote",
        parameters=[ToolParameter(name="stock_code", type="string", description="code")],
        handler=lambda stock_code: {
            "code": stock_code,
            "name": f"{stock_code}名称",
            "price": 10.0,
            "change_pct": 1.2,
            "turnover_rate": 3.2,
            "volume_ratio": 1.1,
            "market_session": "closed_non_trading_day",
            "quote_trade_date": "2026-05-01",
        },
    ))
    registry.register(ToolDefinition(
        name="analyze_trend",
        description="trend",
        parameters=[ToolParameter(name="stock_code", type="string", description="code")],
        handler=lambda stock_code: {
            "code": stock_code,
            "trend_status": "多头排列",
            "bias_ma5": 1.5,
            "support_levels": [9.5],
            "resistance_levels": [11.0],
        },
    ))
    registry.register(ToolDefinition(
        name="analyze_price_structure",
        description="structure",
        parameters=[ToolParameter(name="stock_code", type="string", description="code")],
        handler=lambda stock_code: {
            "code": stock_code,
            "status": "ok",
            "chan": {"pen_count": 3, "center_count": 1, "latest_pens": [{"direction": "up"}]},
            "smc": {"swing_count": 5, "bos": {"status": "bullish"}, "choch": {"status": "none"}},
        },
    ))
    registry.register(ToolDefinition(
        name="get_capital_flow",
        description="flow",
        parameters=[ToolParameter(name="stock_code", type="string", description="code")],
        handler=lambda stock_code: {"stock_code": stock_code, "status": "failed", "errors": ["timeout"]},
    ))
    registry.register(ToolDefinition(
        name="get_stock_info",
        description="info",
        parameters=[ToolParameter(name="stock_code", type="string", description="code")],
        handler=lambda stock_code: {"code": stock_code, "name": f"{stock_code}名称", "belong_boards": []},
    ))
    registry.register(ToolDefinition(
        name="get_chip_distribution",
        description="chip",
        parameters=[ToolParameter(name="stock_code", type="string", description="code")],
        handler=lambda stock_code: {"code": stock_code, "avg_cost": 9.8, "cost_90_low": 9.0},
    ))
    registry.register(ToolDefinition(
        name="search_comprehensive_intel",
        description="intel",
        parameters=[
            ToolParameter(name="stock_code", type="string", description="code"),
            ToolParameter(name="stock_name", type="string", description="name"),
        ],
        handler=lambda stock_code, stock_name: {"report": f"{stock_code} {stock_name} 无重大利空"},
    ))
    registry.register(ToolDefinition(
        name="get_regime_forward_probability",
        description="market regime probability",
        parameters=[],
        handler=lambda **kwargs: {
            "status": "ok",
            "regime": kwargs.get("regime") or "trending_up",
            "sample_count": 20,
            "windows": {"30": {"n": 20, "effective_n": 2, "p_up": 0.55, "p_below_current": 0.4, "low_confidence": False}},
            "reentry_reference": {"reentry_price": 9.6, "low_confidence": False},
        },
    ))
    registry.register(ToolDefinition(
        name="get_symbol_regime_probability",
        description="symbol regime probability",
        parameters=[],
        handler=lambda stock_code, **kwargs: {
            "status": "ok",
            "stock_code": stock_code,
            "regime": kwargs.get("regime") or "trending_up",
            "source": "symbol_ohlc_forward_return_by_market_regime",
            "sample_count": 18,
            "matched_anchor_count": 18,
            "symbol_history_records": 220,
            "windows": {"30": {"n": 18, "effective_n": 2, "p_up": 0.61, "p_below_current": 0.44, "p20_min_return_pct": -4.2, "low_confidence": False}},
            "path_profile": {"window": "90d", "n": 18},
            "reentry_reference": {"current_price": kwargs.get("current_price"), "reentry_price": 9.58, "low_confidence": False},
        },
    ))
    return registry


def _registry_with_candidates(candidates):
    registry = _registry()
    registry.register(ToolDefinition(
        name="discover_watchlist_candidates",
        description="discover",
        parameters=[],
        handler=lambda market="cn", seed_symbols=None, limit=8: {
            "status": "ok",
            "market": market,
            "candidates": candidates[:limit],
        },
    ))
    return registry


def _context():
    return AgentUserContext(
        investor=InvestorProfile(
            risk_preference="balanced",
            trading_horizon="long_term",
            max_single_position_pct=20,
            max_total_equity_exposure_pct=80,
            default_stop_loss_pct=8,
        ),
        accounts=[
            AccountContext(
                account_id=1,
                account_name="5w账户",
                total_equity=50000,
                available_cash=50000,
                total_market_value=0,
            )
        ],
        report=ReportContext(
            analysis_mode="planning_execute",
            intent="watchlist_scan",
            include_watchlist_ranking=True,
        ),
    )


def _context_with_position(symbol: str = "600002"):
    ctx = _context()
    ctx.positions = [
        PositionContext(
            symbol=symbol,
            quantity=100,
            avg_cost=9.5,
            last_price=10.0,
            market_value=1000.0,
            position_pct=2.0,
        )
    ]
    return ctx


def _json_response(payload, tokens=1):
    return LLMResponse(
        content=json.dumps(payload, ensure_ascii=False),
        usage={"total_tokens": tokens},
        provider="openai",
        model="openai/test",
    )


def _deep_dive_response(code, name=None, *, action_bias="wait"):
    name = name or f"{code}名称"
    return _json_response({
        "stage": "single_stock_deep_dive",
        "status": "ok",
        "summary": {
            "code": code,
            "name": name,
            "action_bias": action_bias,
            "action_strength": "weak",
            "main_supporting_evidence": [f"{code} 技术证据"],
        },
        "full": {
            "stock": {"code": code, "name": name},
            "dimension_summary": {
                "technical": {"verdict": "support", "summary": f"{code} 技术结构已取证"}
            },
            "missing_evidence": [],
        },
    })


@pytest.fixture(autouse=True)
def _mock_thesis_desk_committee_discovery():
    """Keep full-pipeline tests on the default thesis-desk path without running it."""

    original = stock_selection_module._run_candidate_discovery_tool

    def _consume_legacy_discovery_response(ctx, llm_adapter):
        call_text = getattr(llm_adapter, "call_text", None)
        if not callable(call_text):
            return None
        try:
            response = call_text(
                [
                    {"role": "system", "content": "测试用三席位候选发现 mock。只输出 JSON。"},
                    {"role": "user", "content": "{}"},
                ],
                timeout=None,
            )
        except (StopIteration, RuntimeError):
            raise
        except Exception:
            return None
        stock_selection_module._accumulate_usage(ctx, response)
        parsed = stock_selection_module.try_parse_json(getattr(response, "content", "") or "")
        return parsed if isinstance(parsed, dict) else None

    def _mocked_run_candidate_discovery_tool(*, ctx, tool_registry, target_symbols, llm_adapter=None):
        if ctx.candidate_discovery_mode != "thesis_desk_committee":
            return original(
                ctx=ctx,
                tool_registry=tool_registry,
                target_symbols=target_symbols,
                llm_adapter=llm_adapter,
            )

        tool_seed = stock_selection_module._execute_tool(
            ctx,
            tool_registry,
            "discover_watchlist_candidates",
            {
                "market": ctx.market,
                "seed_symbols": target_symbols,
                "limit": stock_selection_module.DISCOVERY_RECALL_LIMIT,
            },
        )
        legacy_payload = _consume_legacy_discovery_response(ctx, llm_adapter)
        tool_candidates = tool_seed.get("candidates") if isinstance(tool_seed, dict) else None
        legacy_full = legacy_payload.get("full") if isinstance(legacy_payload, dict) else {}
        legacy_candidates = legacy_full.get("candidates") if isinstance(legacy_full, dict) else None
        candidates = tool_candidates if isinstance(tool_candidates, list) else None
        if not isinstance(candidates, list):
            candidates = legacy_candidates if isinstance(legacy_candidates, list) else []
        elif isinstance(legacy_candidates, list):
            legacy_by_code = {
                str(item.get("code")): item
                for item in legacy_candidates
                if isinstance(item, dict) and item.get("code")
            }
            candidates = [
                {
                    **legacy_by_code.get(str(item.get("code")), {}),
                    **item,
                }
                if isinstance(item, dict) else item
                for item in candidates
            ]
        candidates = candidates if isinstance(candidates, list) else []
        candidate_codes = [
            str(item.get("code"))
            for item in candidates
            if isinstance(item, dict) and item.get("code")
        ]
        key_sources = list(dict.fromkeys(
            str(item.get("source") or "thesis_desk_committee")
            for item in candidates
            if isinstance(item, dict)
        ))[:5]
        summary = dict(legacy_payload.get("summary") or {}) if isinstance(legacy_payload, dict) else {}
        summary.update({
            "strategy": ctx.candidate_strategy,
            "candidate_codes": candidate_codes,
            "candidate_source": "thesis_desk_committee",
            "key_sources": key_sources,
            "source_count": len(candidates),
            "main_limitations": summary.get("main_limitations") or [],
            "next_required_tools": [
                "get_realtime_quote",
                "analyze_trend",
                "analyze_price_structure",
                "get_capital_flow",
                "search_comprehensive_intel",
            ],
        })
        return {
            "stage": "candidate_discovery",
            "status": "ok" if candidates else "insufficient_candidates",
            "strategy": ctx.candidate_strategy,
            "market": ctx.market,
            "candidate_count": len(candidates),
            "candidate_source": "thesis_desk_committee",
            "summary": summary,
            "full": {
                "candidates": candidates,
                "excluded": [],
                "tool_failures": [],
                "missing_evidence": [],
                "thesis_desk_committee": {
                    "status": "ok" if candidates else "insufficient_candidates",
                    "candidate_count": len(candidates),
                    "degraded": False,
                    "dimensions_covered": [
                        "early_turn_desk",
                        "momentum_desk",
                        "quality_repair_desk",
                    ],
                },
                "llm_expert_committee": {
                    "status": "ok" if candidates else "insufficient_candidates",
                    "candidate_count": len(candidates),
                    "degraded": False,
                    "delegate": "thesis_desk_committee",
                },
            },
            "full_ref": "candidate_discovery.json",
        }

    with patch(
        "src.agent.stock_selection._run_candidate_discovery_tool",
        side_effect=_mocked_run_candidate_discovery_tool,
    ):
        yield


def test_stock_selection_report_does_not_promote_weak_wait_or_candidate_score_to_top_pick():
    report = {
        "orchestration_mode": "expert_graph",
        "candidate_discovery": {
            "summary": {"source_count": 4},
            "full": {
                "candidates": [
                    {
                        "code": "000568",
                        "name": "泸州老窖",
                        "source": "fundamental:quality_snapshot",
                        "signal_score": 100,
                        "reason": "基本面预计算表筛选：质量、成长、估值和现金流综合得分较高。",
                    },
                    {
                        "code": "300283",
                        "name": "温州宏丰",
                        "source": "sequoia:multi_strategy",
                        "signal_score": 100,
                        "reason": "多策略共振：turtle_trade, rps_breakout。",
                    },
                ]
            },
        },
        "candidate_screening": {"summary": {}, "full": {}},
        "single_stock_deep_dive": {
            "summary": {
                "target_count": 1,
                "open_targets": [],
                "wait_targets": ["000568"],
                "reject_targets": [],
            },
            "full": {
                "results": [
                    {
                        "summary": {
                            "code": "000568",
                            "name": "泸州老窖",
                            "action_bias": "wait",
                            "action_strength": "weak",
                            "main_risks": ["趋势空头", "资金流出", "数据过期"],
                        },
                        "full": {
                            "stock": {"code": "000568", "name": "泸州老窖"},
                            "action_bias": "wait",
                            "action_strength": "weak",
                            "entry_quality": {
                                "ideal_entry_zone": "",
                                "stop_loss": "",
                            },
                            "key_evidence": ["趋势状态：强势空头。"],
                            "risk_flags": ["技术趋势明确为空头。", "主力资金近期呈净流出状态。", "关键数据严重过期。"],
                            "missing_evidence": ["具体支撑压力位", "最新资金流向"],
                        },
                    }
                ]
            },
        },
        "portfolio_allocation": {
            "summary": {
                "portfolio_action": "wait",
                "recommended_position_count": 0,
                "core_reason": "所有候选标的均处于 wait 或 reject 状态，缺乏足够证据支持立即开仓。",
                "main_constraint": "单仓不超过20%",
            },
            "full": {
                "positions_plan": [
                    {
                        "rank": 1,
                        "code": "000568",
                        "name": "泸州老窖",
                        "action": "wait",
                        "action_strength": "weak",
                        "initial_position_pct": 0,
                        "entry_condition": "等待趋势转为多头。",
                        "stop_loss_condition": "若买入后跌幅达8%止损。",
                        "review_trigger": "每日复查。",
                        "reason": "趋势强势空头，资金持续流出。",
                        "risk_flags": ["趋势空头", "资金流出", "数据过期"],
                    }
                ]
            },
        },
        "adversarial_review": {"summary": {}, "full": {}},
        "judge_decision": {
            "summary": {
                "primary_plan_verdict": "wait_for_more_data",
                "final_action": "wait",
                "decision_summary": "当前不具备建仓条件。",
            },
            "full": {"risk_controls": ["本轮不得新建仓位"]},
        },
    }

    markdown = render_stock_selection_markdown(report)
    recommendation_section = markdown.split("## 三、关键风险与等待确认", 1)[0]

    assert "| 机会首选 | 暂无高质量机会标的 |" in markdown
    assert "| 执行首选 | 暂无可执行标的 |" in markdown
    assert "## 二、深挖结果与等待/排除决策" in recommendation_section
    assert "### ⏳ 等待确认 1：000568 泸州老窖" in recommendation_section
    assert "### 🥇 首选：000568 泸州老窖" not in recommendation_section
    assert "| 理想入场区间 | 当前不形成可执行买点，仅保留观察条件 |" not in recommendation_section
    assert "| 首仓比例 | 0% |" not in recommendation_section
    assert "| 第一目标位 | - |" not in recommendation_section
    assert "| 关注条件 | 等待趋势转为多头。 |" in recommendation_section
    assert "| 失效条件 |" in recommendation_section
    assert "| 复查触发 | 每日复查。 |" in recommendation_section
    assert "突破后回踩不破" not in recommendation_section
    assert "前高或筹码压力位" not in recommendation_section
    assert "### 观察池" in recommendation_section
    assert "| 300283 温州宏丰 | 强观察 |" in recommendation_section


def test_stock_selection_final_markdown_keeps_debate_auxiliary():
    long_judge = (
        "这是一个非常长的 Judge 裁决文本，包含大量反方审查内容、证据缺口、等待理由和组合调整说明。"
        "如果它进入核心原因，就会让最终报告看起来像辩论结果而不是选股报告。"
        "这段文字应该只作为辅助审查摘要出现，并且需要被压缩。"
    )
    report = {
        "orchestration_mode": "expert_graph",
        "candidate_discovery": {
            "summary": {"source_count": 1},
            "full": {
                "candidates": [
                    {"code": "600001", "name": "测试一", "source": "alphasift", "final_score": 88, "reason": "策略入池"}
                ]
            },
        },
        "candidate_screening": {"summary": {}, "full": {}},
        "single_stock_deep_dive": {
            "summary": {},
            "full": {
                "results": [
                    {
                        "summary": {"code": "600001", "name": "测试一", "action_bias": "wait"},
                        "full": {"stock": {"code": "600001", "name": "测试一"}, "missing_evidence": ["资金数据待确认"]},
                    }
                ]
            },
        },
        "portfolio_allocation": {
            "summary": {
                "portfolio_action": "wait",
                "core_reason": "组合主结论：候选质量尚可但等待回踩确认。",
                "main_constraint": "单票不超过40%",
            },
            "full": {"positions_plan": []},
        },
        "adversarial_review": {
            "summary": {
                "opposing_summary": "反方提示：资金和基本面仍需补证，不能把技术突破直接等同于买入。",
                "top_risk_points": ["风险一", "风险二", "风险三", "风险四"],
            },
            "full": {"missing_evidence": ["缺口一", "缺口二", "缺口三", "缺口四"]},
        },
        "judge_decision": {
            "summary": {
                "primary_plan_verdict": "accept_with_changes",
                "final_action": "wait",
                "decision_summary": long_judge,
            },
            "full": {"risk_controls": ["保持等待"]},
        },
    }

    markdown = render_stock_selection_markdown(report)

    conclusion_block = markdown.split("## 五、风控复查摘要", 1)[0]
    assert "组合主结论：候选质量尚可但等待回踩确认。" in conclusion_block
    assert "如果它进入核心原因" not in conclusion_block
    assert "## 五、风控复查摘要" in markdown
    assert "反方审查与 Judge 裁决" not in markdown
    assert "风险三" in markdown
    assert "风险四" not in markdown
    assert "缺口一" in markdown
    assert "缺口三" not in markdown
    assert "缺口四" not in markdown


def test_normalize_deep_dive_payload_overrides_quote_basis_with_real_quote_snapshot():
    fallback = {
        "stage": "single_stock_deep_dive",
        "status": "partial",
        "summary": {
            "code": "603986",
            "name": "兆易创新",
            "action_bias": "reject",
            "action_strength": "none",
            "quote_basis": "latest_trading_day",
            "ideal_entry_zone": "等待回踩或突破确认",
            "main_supporting_evidence": [],
            "main_risks": [],
            "main_missing_evidence": [],
        },
        "full": {
            "stock": {
                "code": "603986",
                "name": "兆易创新",
                "price": 468.74,
                "change_pct": 8.53,
                "quote_trade_date": "2026-05-22",
                "price_label": "最新可用价",
                "change_pct_label": "最近交易日涨跌幅",
                "freshness_note": "查询日市场休市，price/change_pct 为最近可用交易日行情，不代表查询日盘中涨跌。",
                "market_session": "closed_non_trading_day",
            },
            "entry_quality": {
                "ideal_entry_zone": "等待回踩或突破确认",
                "no_chase_line": "",
                "stop_loss": "",
                "failure_condition": "",
            },
            "key_evidence": [],
            "risk_flags": ["盘中追高风险极高"],
            "missing_evidence": [],
        },
        "full_ref": "single_stock_deep_dive_603986.json",
    }
    payload = {
        "stage": "single_stock_deep_dive",
        "status": "ok",
        "summary": {
            "code": "603986",
            "name": "兆易创新",
            "action_bias": "reject",
            "action_strength": "none",
            "quote_basis": "intraday",
            "ideal_entry_zone": "当前价位无法执行，需等待价格回调至账户资金可覆盖范围（约200元以下）及技术面修正",
            "main_supporting_evidence": ["实时行情显示股价单日大涨8.53%，成交活跃（换手率9.55%）"],
            "main_risks": ["盘中追高风险极高"],
            "main_missing_evidence": [],
        },
        "full": {
            "stock": {"code": "603986", "name": "兆易创新"},
            "entry_quality": {
                "ideal_entry_zone": "当前价位无法执行，需等待价格回调至账户资金可覆盖范围（约200元以下）及技术面修正",
                "no_chase_line": "",
                "stop_loss": "",
                "failure_condition": "",
            },
            "key_evidence": ["实时行情显示股价单日大涨8.53%，成交活跃（换手率9.55%）"],
            "risk_flags": ["盘中追高风险极高"],
            "missing_evidence": [],
        },
    }

    normalized = _normalize_deep_dive_payload(payload, fallback=fallback)
    summary = normalized["summary"]
    stock = normalized["full"]["stock"]
    key_evidence = normalized["full"]["key_evidence"]
    risk_flags = normalized["full"]["risk_flags"]

    assert summary["quote_basis"] == "latest_trading_day"
    assert stock["price"] == 468.74
    assert stock["quote_trade_date"] == "2026-05-22"
    assert stock["price_label"] == "最新可用价"
    assert any("最新可用价=468.74（截至2026-05-22）" in item for item in key_evidence)
    assert any("最近交易日涨跌幅=8.53%" in item for item in key_evidence)
    assert any("查询日市场休市" in item for item in key_evidence)
    assert all("盘中" not in item for item in risk_flags)


def test_wait_recommendation_hides_low_signal_template_rows_but_keeps_specific_review_trigger():
    item = {
        "action": "wait",
        "action_strength": "weak",
        "quote_basis": "intraday",
        "initial_position_pct": 0,
        "add_condition": "当前不建议加仓；等待趋势、资金和基本面缺口改善后再评估。",
        "stop_loss_condition": "股价跌破5.00元且无法收回；出现机构大幅减持公告；行业政策突变利空显示面板行业",
        "review_trigger": "股价跌破5.00元且无法收回；出现机构大幅减持公告；行业政策突变利空显示面板行业",
    }

    rows = render_stock_selection_markdown({
        "candidate_discovery": {"summary": {}, "full": {"candidates": []}},
        "candidate_screening": {"summary": {}, "full": {}},
        "single_stock_deep_dive": {
            "summary": {"target_count": 1, "open_targets": [], "wait_targets": ["000725"], "reject_targets": []},
            "full": {"results": [{"summary": {"code": "000725", "name": "京东方Ａ", "action_bias": "wait", "action_strength": "weak"}, "full": {"stock": {"code": "000725", "name": "京东方Ａ"}}}]},
        },
        "portfolio_allocation": {
            "summary": {"portfolio_action": "wait", "recommended_position_count": 0, "core_reason": "等待确认", "main_constraint": "证据不足"},
            "full": {"positions_plan": [{"rank": 1, "code": "000725", "name": "京东方Ａ", **item}]},
        },
        "adversarial_review": {"summary": {}, "full": {}},
        "judge_decision": {"summary": {"primary_plan_verdict": "wait_for_more_data", "final_action": "wait", "decision_summary": "等待确认"}, "full": {}},
    }).split("## 四、Execute 证据摘要", 1)[0]

    assert "当前不形成可执行买点，仅保留观察条件" not in rows
    assert "| 首仓比例 | 0% |" not in rows
    assert "当前不建议加仓；等待趋势、资金和基本面缺口改善后再评估。" not in rows
    assert "| 失效条件 | 股价跌破5.00元且无法收回；出现机构大幅减持公告；行业政策突变利空显示面板行业 |" in rows
    assert "| 复查触发 | 股价跌破5.00元且无法收回；出现机构大幅减持公告；行业政策突变利空显示面板行业 |" in rows


def test_compact_tool_result_for_trace_sanitizes_non_finite_numbers():
    result = {
        "status": "ok",
        "candidates": [
            {
                "code": "688707",
                "name": "振华新材",
                "source": "fundamental:tushare_daily_basic",
                "metrics": {"pe_ttm": float("nan"), "pb": 2.15},
                "raw_source_item": {"pe": float("nan"), "dv_ratio": float("inf")},
            }
        ],
    }

    compact = _compact_tool_result_for_trace(result)
    compact_json = json.dumps(compact, ensure_ascii=False, allow_nan=False)

    assert "NaN" not in compact_json
    assert "Infinity" not in compact_json


def test_should_run_stock_selection_only_for_planning_watchlist():
    assert should_run_stock_selection(_context()) is True
    context = _context()
    context.report.intent = "entry_analysis"
    assert should_run_stock_selection(context) is False


def test_stock_selection_report_humanizes_candidate_sources_and_tags():
    report = {
        "orchestration_mode": "expert_graph",
        "candidate_discovery": {
            "summary": {"source_count": 2},
            "full": {
                "candidates": [
                    {
                        "code": "300572",
                        "name": "安车检测",
                        "source": "alphasift:multi_strategy",
                        "signal_score": 95.87,
                        "reason": "AlphaSift 多策略共振：balanced_alpha, capital_heat, momentum_quality, volume_breakout。",
                        "matched_strategies": ["balanced_alpha", "capital_heat", "momentum_quality", "volume_breakout"],
                        "strategy_tags": ["multi_factor", "balanced", "llm_friendly", "momentum"],
                        "recall_sources": ["alphasift:multi_strategy"],
                        "reason_dimensions": [
                            {"dimension": "strategy", "label": "策略", "detail": "AlphaSift YAML 多因子策略入池：均衡 Alpha、资金热度、动量质量、放量突破"},
                        ],
                    },
                    {
                        "code": "000568",
                        "name": "泸州老窖",
                        "source": "fundamental:quality_snapshot",
                        "signal_score": 100,
                        "reason": "基本面预计算表筛选：质量、成长、估值和现金流综合得分较高。",
                        "matched_strategies": ["fundamental_quality"],
                        "strategy_tags": ["quality", "growth", "value", "fundamental:quality_snapshot"],
                    },
                ],
            },
        },
        "candidate_screening": {"summary": {}, "full": {}},
        "single_stock_deep_dive": {"summary": {}, "full": {"results": []}},
        "portfolio_allocation": {"summary": {"portfolio_action": "wait", "core_reason": "等待确认"}, "full": {"positions_plan": []}},
        "adversarial_review": {"summary": {}, "full": {}},
        "judge_decision": {"summary": {"primary_plan_verdict": "accept", "final_action": "wait", "decision_summary": "等待确认"}, "full": {}},
    }

    markdown = render_stock_selection_markdown(report)
    candidate_section = markdown.split("## 附录二、逐股维度证据展开", 1)[0]

    assert "| 排名 | 股票 | 入池通道 | 入池分 | 深度分析 | 入池理由 | 关注点 |" in markdown
    assert "AlphaSift 多策略共振" in candidate_section
    assert "基本面质量筛选" in candidate_section
    assert "均衡 Alpha" in candidate_section
    assert "资金热度" in candidate_section
    assert "动量质量" in candidate_section
    assert "放量突破" in candidate_section
    assert "alphasift:multi_strategy" not in candidate_section
    assert "balanced_alpha" not in candidate_section
    assert "capital_heat" not in candidate_section
    assert "momentum_quality" not in candidate_section
    assert "volume_breakout" not in candidate_section
    assert "fundamental:quality_snapshot" not in candidate_section


def test_stock_selection_candidate_appendix_sorts_by_entry_score_and_marks_deep_dive_status():
    report = {
        "candidate_discovery": {
            "summary": {"source_count": 3},
            "full": {
                "candidates": [
                    {"code": "300572", "name": "安车检测", "source": "alphasift:multi_strategy", "signal_score": 95.87, "reason": "策略入池"},
                    {"code": "301183", "name": "东田微", "source": "sequoia:multi_strategy", "signal_score": 100.0, "reason": "技术入池"},
                    {"code": "000568", "name": "泸州老窖", "source": "fundamental:quality_snapshot", "signal_score": 100.0, "reason": "基本面入池"},
                ],
            },
        },
        "candidate_screening": {"summary": {}, "full": {}},
        "single_stock_deep_dive": {
            "summary": {},
            "full": {
                "results": [
                    {
                        "summary": {"code": "000568", "name": "泸州老窖", "action_bias": "wait", "action_strength": "weak"},
                        "full": {"stock": {"code": "000568", "name": "泸州老窖"}, "risk_flags": ["趋势空头"]},
                    }
                ]
            },
        },
        "portfolio_allocation": {"summary": {"portfolio_action": "wait", "core_reason": "等待确认"}, "full": {"positions_plan": []}},
        "adversarial_review": {"summary": {}, "full": {}},
        "judge_decision": {"summary": {"primary_plan_verdict": "accept", "final_action": "wait", "decision_summary": "等待确认"}, "full": {}},
    }

    markdown = render_stock_selection_markdown(report)
    candidate_section = markdown.split("## 附录二、候选池来源与入池理由", 1)[1].split("## 附录三", 1)[0]

    assert "| 排名 | 股票 | 入池通道 | 入池分 | 深度分析 | 入池理由 | 关注点 |" in candidate_section
    assert candidate_section.index("| 1 | 301183 东田微") < candidate_section.index("| 2 | 000568 泸州老窖")
    assert candidate_section.index("| 2 | 000568 泸州老窖") < candidate_section.index("| 3 | 300572 安车检测")
    assert "| 2 | 000568 泸州老窖 | 基本面质量筛选 | 100 | 已完成 |" in candidate_section
    assert "未覆盖（深挖上限 4 只）" in candidate_section
    assert "llm_friendly" not in candidate_section


def test_stock_selection_pipeline_runs_all_stages_with_summaries():
    adapter = MagicMock()
    adapter.call_text.side_effect = [
        _json_response({
            "stage": "candidate_discovery",
            "status": "ok",
            "strategy": "hot_sector",
            "market": "cn",
            "candidate_count": 2,
            "summary": {"candidate_codes": ["600001", "600002"], "main_limitations": []},
            "full": {"candidates": [{"code": "600001", "name": "测试一"}, {"code": "600002", "name": "测试二"}], "missing_evidence": []},
            "full_ref": None,
        }),
        _json_response({
            "stage": "candidate_screening",
            "status": "ok",
            "summary": {"deep_dive_targets": ["600001"], "monitor_targets": [], "rejected_targets": [], "main_limitations": []},
            "full": {"shortlist": [{"code": "600001", "name": "测试一", "screening_result": "deep_dive", "score": 80, "score_breakdown": {}}]},
            "full_ref": None,
        }),
        _json_response({
            "stage": "single_stock_deep_dive",
            "status": "ok",
            "summary": {
                "code": "600001",
                "name": "测试一",
                "action_bias": "wait",
                "action_strength": "weak",
                "no_chase_line": "11",
                "stop_loss": "9",
                "main_supporting_evidence": ["技术面多头排列但需要资金确认"],
                "main_risks": ["资金流工具失败"],
                "main_missing_evidence": ["capital_flow"],
            },
            "full": {
                "stock": {"code": "600001", "name": "测试一", "market": "cn", "data_status": "ok"},
                "action_bias": "wait",
                "entry_quality": {"no_chase_line": "11", "stop_loss": "9"},
                "dimension_summary": {
                    "technical": {"verdict": "support", "summary": "MA5/MA20 多头，价格仍需回踩确认。"},
                    "capital_flow": {"verdict": "tool_failed", "summary": "get_capital_flow timeout，无法确认主力同步。"},
                },
                "key_evidence": ["技术面多头排列"],
                "risk_flags": ["资金流工具失败"],
                "failure_conditions": ["跌破 9 元支撑"],
                "missing_evidence": ["capital_flow"],
            },
            "full_ref": None,
        }),
        _json_response({"stage": "meta_orchestrator", "status": "ok", "summary": {"package_count": 1, "asset_regimes": [{"code": "600001", "asset_regime": "Unknown"}], "market_context_note": "趋势市但需等待确认", "main_constraints": [{"code": "600001", "constraint_type": "invalidation_level", "reason": "9"}]}, "full": {"packages": [{"stock": {"code": "600001", "name": "测试一", "market": "cn"}, "meta_analysis": {"asset_regime": "Unknown"}, "hard_constraints_for_pricing_agent": {"invalidation_level": {"price": 9, "reason": "跌破 9 元支撑"}, "mean_reversion_anchor": {"price": 10, "reason": "回踩确认"}, "max_chase_premium": {"value": "2.0%", "reason": "11"}, "risk_constraints": []}, "required_pricing_scenarios": [{"scenario_name": "Breakout_Continuation", "condition": "回踩确认", "required_output": "条件开仓"}]}]}}),
        _json_response({"stage": "pricing_agent", "status": "ok", "summary": {"priced_count": 1, "tradable_count": 0, "main_pricing_constraints": ["9"], "pricing_note": "等待确认"}, "full": {"if_then_order_matrix": [{"code": "600001", "name": "测试一", "asset_regime": "Unknown", "selected_scenario": "Breakout_Continuation", "scenarios": [{"scenario_name": "Breakout_Continuation", "condition": "回踩确认", "action": "wait", "execution_mode": "plain_wait", "entry_zone": "回踩确认", "stop_loss": "9", "failure_condition": "跌破 9 元支撑", "risk_reward_comment": "等待确认", "constraints_used": ["9"]}]}]}}),
        _json_response({
            "stage": "portfolio_allocation",
            "status": "ok",
            "summary": {"portfolio_action": "wait", "recommended_position_count": 0, "initial_total_position_pct": 0, "reserved_cash_pct": 100, "core_reason": "等待确认", "main_constraint": "资金面缺失"},
            "full": {"positions_plan": [{"rank": 1, "code": "600001", "name": "测试一", "action": "wait", "initial_position_pct": 0, "entry_condition": "回踩确认", "stop_loss_condition": "9", "review_trigger": "下个交易日"}], "risk_controls": []},
            "full_ref": None,
        }),
        _json_response({
            "stage": "adversarial_review",
            "status": "ok",
            "summary": {"opposing_summary": "资金面缺失，不应立即买入", "top_risk_points": ["资金面缺失"], "top_evidence_gaps": ["capital_flow"], "recommended_verdict": "accept_with_changes"},
            "full": {"opposing_thesis": {"risk_points": ["资金面缺失"]}, "missing_evidence": ["capital_flow"]},
            "full_ref": None,
        }),
        _json_response({
            "stage": "judge_decision",
            "status": "ok",
            "summary": {"primary_plan_verdict": "accept_with_changes", "final_action": "wait", "decision_summary": "等待确认", "next_step": "render_final_report"},
            "full": {"winner": "mixed", "risk_controls": ["不追高"]},
            "full_ref": None,
        }),
    ]

    result = run_stock_selection_pipeline(
        task="我现在有5w元，帮我选股",
        agent_user_context=_context(),
        tool_registry=_registry_with_candidates([{"code": "600001", "name": "测试一", "market": "cn", "source": "test"}]),
        llm_adapter=adapter,
        run_id="test-run",
        orchestration_mode="legacy",
    )

    assert result.success is True
    assert "选股分析报告：下周可关注候选" in result.final_markdown
    assert "核心推荐结论" in result.final_markdown
    assert "深挖结果与等待/排除决策" in result.final_markdown
    assert "Execute 证据摘要" not in result.final_markdown
    assert "候选池来源与入池理由" in result.final_markdown or "候选池来源与入池理由" in result.appendix_markdown
    assert "逐股维度证据展开" in result.final_markdown or "逐股维度证据展开" in result.appendix_markdown
    assert "600001 测试一" in result.final_markdown
    combined_markdown = result.final_markdown + "\n" + result.appendix_markdown
    assert "MA5/MA20 多头" in combined_markdown
    assert "跌破 9 元支撑" in combined_markdown
    assert "capital_flow" in combined_markdown
    assert result.context.stage_summary("judge_decision")["final_action"] == "wait"
    assert result.context.stages["candidate_discovery"].full_ref == "candidate_discovery.json"
    assert adapter.call_text.call_count == 8
    assert result.total_tokens == 8
    assert any(call["tool"] == "discover_watchlist_candidates" for call in result.tool_calls_log)
    assert any(call["tool"] == "detect_market_regime" for call in result.tool_calls_log)
    assert result.final_report_json["market_regime"]["regime"] == "trending_up"
    assert result.final_report_json["orchestration_mode"] == "legacy"
    assert result.final_report_json["expert_state"] is None
    capital_flow_calls = [call for call in result.tool_calls_log if call["tool"] == "get_capital_flow"]
    assert capital_flow_calls
    assert all(call["success"] is False for call in capital_flow_calls)
    assert "get_capital_flow" in result.context.evidence_ledger["summary"]["failed_tools"]


def test_stock_selection_resume_reuses_complete_candidate_discovery_artifact(tmp_path):
    resume_dir = tmp_path / "trace-old"
    resume_dir.mkdir()
    (resume_dir / "candidate_discovery.json").write_text(json.dumps({
        "status": "ok",
        "summary": {"candidate_codes": ["600001"], "main_limitations": []},
        "full_ref": "candidate_discovery.json",
        "full": {
            "candidates": [{"code": "600001", "name": "测试一", "market": "cn", "source": "resume"}],
            "missing_evidence": [],
        },
    }, ensure_ascii=False), encoding="utf-8")

    adapter = MagicMock()
    adapter.call_text.side_effect = [
        _json_response({
            "stage": "candidate_screening",
            "status": "ok",
            "summary": {"deep_dive_targets": ["600001"], "monitor_targets": [], "rejected_targets": [], "main_limitations": []},
            "full": {"shortlist": [{"code": "600001", "name": "测试一", "screening_result": "deep_dive", "score": 80}]},
        }),
        _json_response({
            "stage": "single_stock_deep_dive",
            "status": "ok",
            "summary": {"code": "600001", "name": "测试一", "action_bias": "wait", "action_strength": "weak"},
            "full": {"stock": {"code": "600001", "name": "测试一"}, "action_bias": "wait", "entry_quality": {}, "dimension_summary": {}, "missing_evidence": []},
        }),
        _json_response({"stage": "meta_orchestrator", "status": "ok", "summary": {"package_count": 1}, "full": {"packages": []}}),
        _json_response({"stage": "pricing_agent", "status": "ok", "summary": {"priced_count": 0}, "full": {"if_then_order_matrix": []}}),
        _json_response({"stage": "portfolio_allocation", "status": "ok", "summary": {"portfolio_action": "wait"}, "full": {"positions_plan": []}}),
        _json_response({"stage": "adversarial_review", "status": "ok", "summary": {"opposing_summary": "等待"}, "full": {}}),
        _json_response({"stage": "judge_decision", "status": "ok", "summary": {"final_action": "wait", "primary_plan_verdict": "accept_with_changes", "decision_summary": "等待", "next_step": "render_final_report"}, "full": {}}),
    ]
    events = []

    result = run_stock_selection_pipeline(
        task="继续选股",
        agent_user_context=_context(),
        tool_registry=_registry_with_candidates([{"code": "999999", "name": "不应召回", "market": "cn"}]),
        llm_adapter=adapter,
        run_id="trace-new",
        orchestration_mode="legacy",
        candidate_discovery_mode="thesis_desk_committee",
        resume_artifact_dir=str(resume_dir),
        resume_from_run_id="trace-old",
        progress_callback=events.append,
    )

    assert result.success is True
    assert result.context.stage_full("candidate_discovery")["candidates"][0]["source"] == "resume"
    assert not any(call["tool"] == "discover_watchlist_candidates" for call in result.tool_calls_log)
    assert any(event["type"] == "selection_resume_loaded" for event in events)
    assert any(
        event["type"] == "selection_resume_reused_stage"
        and event.get("payload", {}).get("stage") == "candidate_discovery"
        for event in events
    )


def test_stock_selection_prompts_use_compact_evidence_cards_not_raw_previews():
    registry = _registry()
    huge_blob = "RAW_UNCOMPRESSED_PAYLOAD_" * 500
    registry.register(ToolDefinition(
        name="discover_watchlist_candidates",
        description="discover",
        parameters=[],
        handler=lambda market="cn", seed_symbols=None, limit=8: {
            "status": "ok",
            "market": market,
            "candidate_source": "alphasift",
            "candidates": [
                {
                    "code": "600001",
                    "name": "测试一",
                    "source": "alphasift:multi_strategy",
                    "signal_score": 88,
                    "reason": "AlphaSift 多因子入池",
                    "raw_metrics": huge_blob,
                }
            ],
            "diagnostics": {"raw_dump": huge_blob},
        },
    ))
    registry.register(ToolDefinition(
        name="get_stock_info",
        description="info",
        parameters=[ToolParameter(name="stock_code", type="string", description="code")],
        handler=lambda stock_code: {
            "code": stock_code,
            "name": "测试一",
            "belong_boards": [],
            "raw_profile": huge_blob,
        },
    ))
    adapter = MagicMock()
    adapter.call_text.side_effect = [
        _json_response({"stage": "candidate_discovery", "status": "ok", "summary": {"candidate_codes": ["600001"]}, "full": {"candidates": [{"code": "600001", "name": "测试一"}]}}),
        _json_response({"stage": "candidate_screening", "status": "ok", "summary": {"deep_dive_targets": ["600001"]}, "full": {"shortlist": [{"code": "600001", "name": "测试一"}]}}),
        _json_response({"stage": "single_stock_deep_dive", "status": "ok", "summary": {"code": "600001", "name": "测试一", "action_bias": "wait"}, "full": {"stock": {"code": "600001", "name": "测试一"}}}),
        _json_response({"stage": "portfolio_allocation", "status": "ok", "summary": {"portfolio_action": "wait", "core_reason": "等待"}, "full": {"positions_plan": []}}),
        _json_response({"stage": "adversarial_review", "status": "ok", "summary": {"opposing_summary": "等待"}, "full": {"opposing_thesis": {}}}),
        _json_response({"stage": "judge_decision", "status": "ok", "summary": {"primary_plan_verdict": "accept", "final_action": "wait", "decision_summary": "等待", "next_step": "render_final_report"}, "full": {"winner": "mixed"}}),
    ]

    result = run_stock_selection_pipeline(
        task="帮我选一下下周可以入手的股票",
        agent_user_context=_context(),
        tool_registry=registry,
        llm_adapter=adapter,
        run_id="test-compact-prompt",
    )

    assert result.success is True
    prompts = [call.args[0][1]["content"] for call in adapter.call_text.call_args_list]
    assert all("RAW_UNCOMPRESSED_PAYLOAD" not in prompt for prompt in prompts)
    assert all('"preview"' not in prompt for prompt in prompts[1:])
    assert any('"evidence_cards"' in prompt for prompt in prompts)
    assert any('"raw_policy"' in prompt for prompt in prompts)


def test_stock_selection_attaches_symbol_regime_probability_for_deep_dive_and_holdings():
    adapter = MagicMock()
    adapter.call_text.side_effect = [
        _json_response({"stage": "candidate_discovery", "status": "ok", "summary": {"candidate_codes": ["600001"]}, "full": {"candidates": [{"code": "600001", "name": "测试一"}]}}),
        _json_response({"stage": "candidate_screening", "status": "ok", "summary": {"deep_dive_targets": ["600001"]}, "full": {"shortlist": [{"code": "600001", "name": "测试一"}]}}),
        _json_response({"stage": "single_stock_deep_dive", "status": "ok", "summary": {"code": "600001", "name": "测试一", "action_bias": "wait"}, "full": {"stock": {"code": "600001", "name": "测试一"}}}),
        _json_response({"stage": "meta_orchestrator", "status": "ok", "summary": {"package_count": 1}, "full": {"packages": []}}),
        _json_response({"stage": "pricing_agent", "status": "ok", "summary": {"priced_count": 0}, "full": {"if_then_order_matrix": []}}),
        _json_response({"stage": "portfolio_allocation", "status": "ok", "summary": {"portfolio_action": "wait", "core_reason": "等待"}, "full": {"positions_plan": []}}),
        _json_response({"stage": "adversarial_review", "status": "ok", "summary": {"opposing_summary": "等待"}, "full": {"opposing_thesis": {}}}),
        _json_response({"stage": "judge_decision", "status": "ok", "summary": {"primary_plan_verdict": "accept", "final_action": "wait", "decision_summary": "等待", "next_step": "render_final_report"}, "full": {"winner": "mixed"}}),
    ]

    result = run_stock_selection_pipeline(
        task="帮我选一下下周可以入手的股票",
        agent_user_context=_context_with_position("600002"),
        tool_registry=_registry(),
        llm_adapter=adapter,
        run_id="test-symbol-regime-probability",
    )

    assert result.success is True
    stage = result.final_report_json["symbol_regime_probability"]
    assert stage["summary"]["scope_counts"] == {"deep_dive": 1, "holding": 1}
    codes = {item["stock_code"] for item in stage["summary"]["results_brief"]}
    assert codes == {"600001", "600002"}
    deep_full = result.final_report_json["single_stock_deep_dive"]["full"]["results"][0]["full"]
    assert deep_full["symbol_regime_probability"]["stock_code"] == "600001"
    prompts = [call.args[0][1]["content"] for call in adapter.call_text.call_args_list]
    allocation_prompt = next(prompt for prompt in prompts if "组合配置 Agent" in prompt)
    assert '"symbol_regime_probabilities"' in allocation_prompt
    symbol_calls = [call for call in result.tool_calls_log if call["tool"] == "get_symbol_regime_probability"]
    assert [call["arguments"]["stock_code"] for call in symbol_calls] == ["600001", "600002"]


def test_stock_selection_runs_meta_and_pricing_stages_before_allocation():
    adapter = MagicMock()
    adapter.call_text.side_effect = [
        _json_response({"stage": "candidate_discovery", "status": "ok", "summary": {"candidate_codes": ["600001"]}, "full": {"candidates": [{"code": "600001", "name": "测试一", "setup_type": "trend_continuation", "primary_desk": "momentum_desk", "desks": ["momentum_desk", "quality_repair_desk"], "reason": "动量席位认为突破有效", "risks": ["质量席位担心高位衰竭"]}]}}),
        _json_response({"stage": "candidate_screening", "status": "ok", "summary": {"deep_dive_targets": ["600001"]}, "full": {"shortlist": [{"code": "600001", "name": "测试一", "screening_result": "deep_dive"}]}}),
        _json_response({"stage": "single_stock_deep_dive", "status": "ok", "summary": {"code": "600001", "name": "测试一", "action_bias": "wait", "action_strength": "medium", "ideal_entry_zone": "回踩 10 元确认", "no_chase_line": "高于 10.6 不追", "stop_loss": "跌破 9.7", "main_risks": ["高位衰竭"]}, "full": {"stock": {"code": "600001", "name": "测试一", "market": "cn"}, "entry_quality": {"ideal_entry_zone": "回踩 10 元确认", "no_chase_line": "高于 10.6 不追", "stop_loss": "跌破 9.7", "failure_condition": "放量跌破突破位"}}}),
        _json_response({
            "stage": "meta_orchestrator",
            "status": "ok",
            "summary": {"package_count": 1, "asset_regimes": [{"code": "600001", "asset_regime": "Right_Side_Momentum_High_Exhaustion_Risk"}], "market_context_note": "趋势市但不追高", "main_constraints": [{"code": "600001", "constraint_type": "invalidation_level", "reason": "跌破 9.7"}]},
            "full": {"packages": [{"stock": {"code": "600001", "name": "测试一", "market": "cn"}, "meta_analysis": {"asset_regime": "Right_Side_Momentum_High_Exhaustion_Risk", "factual_consensus": ["回踩 10 元确认"], "strategic_divergence": "动量看多，质量担心高位", "dominant_thesis": "momentum_desk", "opposing_theses": ["quality_repair_desk"]}, "market_context": {"market_regime": "trending_up", "volatility_bucket": "normal", "risk_level": "medium", "regime_weight_adjustment": "趋势市保留动量", "market_context_warnings": []}, "hard_constraints_for_pricing_agent": {"invalidation_level": {"price": 9.7, "source": "deep_dive", "reason": "跌破 9.7"}, "mean_reversion_anchor": {"price": 10.0, "source": "deep_dive", "reason": "回踩 10 元确认"}, "max_chase_premium": {"value": "2.0%", "source": "quality_repair_desk", "reason": "高于 10.6 不追"}, "risk_constraints": [{"constraint": "高位衰竭", "source": "quality_repair_desk"}]}, "required_pricing_scenarios": [{"scenario_name": "Breakout_Continuation", "condition": "站稳 9.7", "required_output": "条件开仓"}, {"scenario_name": "Fakeout_Exhaustion", "condition": "跌破 9.7", "required_output": "退出"}, {"scenario_name": "Mean_Reversion_Pullback", "condition": "回踩 10", "required_output": "低吸"}]}]},
        }),
        _json_response({
            "stage": "pricing_agent",
            "status": "ok",
            "summary": {"priced_count": 1, "tradable_count": 1, "main_pricing_constraints": ["跌破 9.7"], "pricing_note": "只给条件单"},
            "full": {"if_then_order_matrix": [{"code": "600001", "name": "测试一", "asset_regime": "Right_Side_Momentum_High_Exhaustion_Risk", "data_status": "ok", "selected_scenario": "Breakout_Continuation", "scenarios": [{"scenario_name": "Breakout_Continuation", "condition": "站稳 9.7 后回踩确认", "action": "wait", "execution_mode": "conditional_open", "entry_zone": "10.0-10.2", "stop_loss": "跌破 9.7", "failure_condition": "放量跌破 9.7", "risk_reward_comment": "需复核 ATR", "constraints_used": ["invalidation_level"]}], "pricing_warnings": []}]},
        }),
        _json_response({"stage": "portfolio_allocation", "status": "ok", "summary": {"portfolio_action": "wait", "core_reason": "遵守条件单", "positions_plan_brief": [{"code": "600001", "action": "wait"}]}, "full": {"positions_plan": [{"rank": 1, "code": "600001", "name": "测试一", "action": "wait", "execution_mode": "conditional_open", "initial_position_pct": 0, "entry_condition": "站稳 9.7 后回踩确认", "stop_loss_condition": "跌破 9.7", "review_trigger": "明日"}], "execution_matrix": []}}),
        _json_response({"stage": "adversarial_review", "status": "ok", "summary": {"opposing_summary": "需确认条件单"}, "full": {"opposing_thesis": {}}}),
        _json_response({"stage": "judge_decision", "status": "ok", "summary": {"primary_plan_verdict": "accept_with_changes", "final_action": "wait", "decision_summary": "等待条件触发", "next_step": "render_final_report"}, "full": {"winner": "mixed"}}),
    ]

    result = run_stock_selection_pipeline(
        task="帮我选一下下周可以入手的股票",
        agent_user_context=_context(),
        tool_registry=_registry_with_candidates([{"code": "600001", "name": "测试一", "market": "cn", "source": "test"}]),
        llm_adapter=adapter,
        run_id="test-meta-pricing",
    )

    assert result.success is True
    assert result.final_report_json["meta_orchestrator"]["summary"]["package_count"] == 1
    assert result.final_report_json["pricing_agent"]["summary"]["priced_count"] == 1
    assert result.context.stages["meta_orchestrator"].full_ref == "meta_orchestrator.json"
    assert result.context.stages["pricing_agent"].full_ref == "pricing_agent.json"
    prompts = [call.args[0][1]["content"] for call in adapter.call_text.call_args_list]
    assert any("Meta-Agent / Orchestrator" in prompt for prompt in prompts)
    assert any("点位计算 Agent / 条件单计算层" in prompt for prompt in prompts)
    assert any("Meta/点位计算字段语义" in prompt for prompt in prompts)
    assert "## 附录一、系统推理链路（调试用）" in result.appendix_markdown
    assert "字段说明" not in result.final_markdown
    assert "右侧动量，但短线过热" in result.appendix_markdown
    assert "点位条件单" in result.appendix_markdown
    assert "Meta 场景约束" not in result.final_markdown
    assert "点位计算条件单" not in result.final_markdown
    assert "Pricing If-Then" not in result.final_markdown


def test_meta_and_point_calc_prompt_payloads_are_capped_and_ranked():
    ctx = SelectionRunContext(run_id="test-compact-meta-point", user_message="选股")
    packages = []
    matrix = []
    for idx in range(6):
        code = f"60000{idx}"
        packages.append({
            "stock": {"code": code, "name": f"测试{idx}", "market": "cn"},
            "meta_analysis": {
                "asset_regime": "Right_Side_Momentum",
                "factual_consensus": [f"事实{idx}-{n}" for n in range(6)],
                "strategic_divergence": "分歧" * 200,
            },
            "market_context": {"market_regime": "trending_up"},
            "hard_constraints_for_pricing_agent": {
                "invalidation_level": {"price": 9 + idx, "reason": "跌破失效"},
                "risk_constraints": [{"constraint": f"风险{n}"} for n in range(8)],
            },
            "required_pricing_scenarios": [
                {"scenario_name": f"S{n}", "condition": "条件" * 80, "required_output": "输出" * 80}
                for n in range(5)
            ],
        })
        execution_mode = "conditional_open" if idx == 4 else "plain_wait"
        matrix.append({
            "code": code,
            "name": f"测试{idx}",
            "asset_regime": "Right_Side_Momentum",
            "data_status": "ok",
            "selected_scenario": "S0",
            "scenarios": [
                {
                    "scenario_name": f"S{n}",
                    "action": "wait",
                    "execution_mode": execution_mode,
                    "condition": "条件" * 80,
                    "entry_zone": "10-10.2",
                    "stop_loss": "9.7",
                    "failure_condition": "跌破 9.7",
                    "risk_reward_comment": "复核 ATR",
                    "constraints_used": [f"c{x}" for x in range(6)],
                }
                for n in range(5)
            ],
        })
    ctx.set_stage("meta_orchestrator", {"stage": "meta_orchestrator", "status": "ok", "full": {"packages": packages}})
    ctx.set_stage("pricing_agent", {"stage": "pricing_agent", "status": "ok", "full": {"if_then_order_matrix": matrix}})

    compact_meta = stock_selection_module._compact_meta_packages_for_prompt(ctx)
    compact_matrix = stock_selection_module._compact_pricing_matrix_for_prompt(ctx)

    assert len(compact_meta) == 5
    assert len(compact_matrix) == 5
    assert compact_meta[0]["stock"]["code"] == "600004"
    assert compact_matrix[0]["code"] == "600004"
    assert all(len(item["required_pricing_scenarios"]) <= 3 for item in compact_meta)
    assert all(len(item["scenarios"]) <= 3 for item in compact_matrix)
    assert all(len(scenario["constraints_used"]) <= 4 for item in compact_matrix for scenario in item["scenarios"])


def test_stock_selection_emits_tool_events_for_trace_stream():
    adapter = MagicMock()
    adapter.call_text.side_effect = [
        _json_response({"stage": "candidate_discovery", "status": "ok", "summary": {"candidate_codes": ["600001"]}, "full": {"candidates": [{"code": "600001", "name": "测试一"}]}}),
        _json_response({"stage": "candidate_screening", "status": "ok", "summary": {"deep_dive_targets": ["600001"]}, "full": {"shortlist": [{"code": "600001", "name": "测试一"}]}}),
        _json_response({"stage": "single_stock_deep_dive", "status": "ok", "summary": {"code": "600001", "name": "测试一", "action_bias": "wait"}, "full": {"stock": {"code": "600001", "name": "测试一"}}}),
        _json_response({"stage": "portfolio_allocation", "status": "ok", "summary": {"portfolio_action": "wait", "core_reason": "等待"}, "full": {"positions_plan": []}}),
        _json_response({"stage": "adversarial_review", "status": "ok", "summary": {"opposing_summary": "等待"}, "full": {"opposing_thesis": {}}}),
        _json_response({"stage": "judge_decision", "status": "ok", "summary": {"primary_plan_verdict": "accept", "final_action": "wait", "decision_summary": "等待", "next_step": "render_final_report"}, "full": {"winner": "mixed"}}),
    ]
    events = []

    result = run_stock_selection_pipeline(
        task="帮我选一下下周可以入手的股票",
        agent_user_context=_context(),
        tool_registry=_registry_with_candidates([{"code": "600001", "name": "测试一", "market": "cn", "source": "alphasift:multi_strategy"}]),
        llm_adapter=adapter,
        run_id="test-tool-events",
        progress_callback=events.append,
    )

    assert result.success is True
    tool_start_events = [event for event in events if event.get("type") == "tool_start"]
    tool_done_events = [event for event in events if event.get("type") == "tool_done"]
    assert tool_start_events
    assert tool_done_events
    assert tool_done_events[0]["tool"] == "discover_watchlist_candidates"
    assert tool_done_events[0]["selection_stage"] is True
    assert "result_preview" in tool_done_events[0]
    assert "result_json" in tool_done_events[0]
    assert len(tool_done_events) == len(result.tool_calls_log)


def test_stock_selection_reject_is_downgraded_when_candidate_pool_exists_with_data_gaps():
    adapter = MagicMock()
    adapter.call_text.side_effect = [
        _json_response({
            "stage": "candidate_discovery",
            "status": "ok",
            "summary": {"candidate_codes": ["600001"], "main_limitations": []},
            "full": {
                "candidates": [
                    {
                        "code": "600001",
                        "name": "测试一",
                        "source": "alphasift:multi_strategy",
                        "signal_score": 82,
                        "reason": "AlphaSift YAML 多因子策略入池。",
                    }
                ],
                "missing_evidence": [],
            },
        }),
        _json_response({
            "stage": "candidate_screening",
            "status": "ok",
            "summary": {"deep_dive_targets": ["600001"], "main_limitations": []},
            "full": {"shortlist": [{"code": "600001", "name": "测试一", "screening_result": "deep_dive", "score": 80}]},
        }),
        _json_response({
            "stage": "single_stock_deep_dive",
            "status": "ok",
            "summary": {"code": "600001", "name": "测试一", "action_bias": "wait", "action_strength": "weak", "key_reason": "资金面待确认"},
            "full": {"stock": {"code": "600001", "name": "测试一"}, "action_bias": "wait", "missing_evidence": ["capital_flow", "fundamental"]},
        }),
        _json_response({"stage": "meta_orchestrator", "status": "partial", "summary": {"package_count": 1, "asset_regimes": [{"code": "600001", "asset_regime": "Unknown"}], "market_context_note": "证据缺口较多", "main_constraints": []}, "full": {"packages": [{"stock": {"code": "600001", "name": "测试一", "market": "cn"}, "meta_analysis": {"asset_regime": "Unknown"}, "hard_constraints_for_pricing_agent": {"risk_constraints": [{"constraint": "资金面待确认", "source": "deep_dive"}]}, "required_pricing_scenarios": []}]}}),
        _json_response({"stage": "pricing_agent", "status": "partial", "summary": {"priced_count": 1, "tradable_count": 0, "main_pricing_constraints": ["资金面待确认"], "pricing_note": "不形成条件开仓"}, "full": {"if_then_order_matrix": [{"code": "600001", "name": "测试一", "asset_regime": "Unknown", "selected_scenario": "Mean_Reversion_Pullback", "scenarios": [{"scenario_name": "Mean_Reversion_Pullback", "condition": "资金确认后复查", "action": "monitor", "execution_mode": "plain_wait", "entry_zone": "等待", "stop_loss": "跌破支撑", "failure_condition": "资金继续缺失", "risk_reward_comment": "证据不足", "constraints_used": ["资金面待确认"]}]}]}}),
        _json_response({
            "stage": "portfolio_allocation",
            "status": "ok",
            "summary": {"portfolio_action": "wait", "core_reason": "等待确认", "main_constraint": "证据不足"},
            "full": {"positions_plan": [{"rank": 1, "code": "600001", "name": "测试一", "action": "wait", "initial_position_pct": 0, "entry_condition": "资金确认后复查", "stop_loss_condition": "跌破支撑", "review_trigger": "下一交易日"}]},
        }),
        _json_response({
            "stage": "adversarial_review",
            "status": "ok",
            "summary": {"opposing_summary": "证据缺口较多", "top_risk_points": ["资金面缺失"], "top_evidence_gaps": ["capital_flow", "fundamental"], "recommended_verdict": "reject"},
            "full": {"missing_evidence": ["capital_flow", "fundamental"]},
        }),
        _json_response({
            "stage": "judge_decision",
            "status": "ok",
            "summary": {
                "primary_plan_verdict": "reject",
                "final_action": "reject",
                "decision_summary": "证据缺口较多，拒绝本轮建仓。",
                "next_step": "stop_no_trade",
            },
            "full": {"winner": "opposing", "risk_controls": ["不追高"]},
        }),
    ]

    result = run_stock_selection_pipeline(
        task="帮我选一下下周可以入手的股票",
        agent_user_context=_context(),
        tool_registry=_registry_with_candidates([{"code": "600001", "name": "测试一", "market": "cn", "source": "alphasift:multi_strategy"}]),
        llm_adapter=adapter,
        run_id="test-reject-downgrade",
        orchestration_mode="legacy",
    )

    judge = result.final_report_json["judge_decision"]["summary"]
    assert judge["final_action"] == "wait"
    assert judge["primary_plan_verdict"] == "wait_for_more_data"
    assert judge["next_step"] == "render_final_report"
    assert "候选池已形成" in judge["decision_summary"]
    assert "候选池来源与入池理由" in result.final_markdown or "候选池来源与入池理由" in result.appendix_markdown
    assert "AlphaSift 多因子策略入池" in result.final_markdown or "AlphaSift 多因子策略入池" in result.appendix_markdown


def test_stock_selection_expands_deep_dive_targets_for_rich_candidate_pool():
    registry = _registry()
    registry.register(ToolDefinition(
        name="discover_watchlist_candidates",
        description="discover",
        parameters=[],
        handler=lambda market="cn", seed_symbols=None, limit=8: {
            "status": "ok",
            "market": market,
            "candidates": [
                {"code": "600001", "name": "测试一", "market": "cn", "source": "alphasift"},
                {"code": "600002", "name": "测试二", "market": "cn", "source": "sequoia"},
                {"code": "600003", "name": "测试三", "market": "cn", "source": "fundamental"},
                {"code": "600004", "name": "测试四", "market": "cn", "source": "capital"},
            ][:limit],
        },
    ))
    adapter = MagicMock()
    adapter.call_text.side_effect = [
        _json_response({
            "stage": "candidate_discovery",
            "status": "ok",
            "summary": {"candidate_codes": ["600001", "600002", "600003", "600004"]},
            "full": {"candidates": [{"code": "600001"}, {"code": "600002"}, {"code": "600003"}, {"code": "600004"}]},
        }),
        _json_response({
            "stage": "candidate_screening",
            "status": "ok",
            "summary": {"deep_dive_targets": ["600001"]},
            "full": {
                "shortlist": [
                    {"code": "600001", "screening_result": "deep_dive", "score": 90},
                    {"code": "600002", "screening_result": "monitor", "score": 70},
                    {"code": "600003", "screening_result": "deep_dive", "score": 68},
                    {"code": "600004", "screening_result": "reject", "score": 20},
                ]
            },
        }),
        _json_response({"stage": "single_stock_deep_dive", "status": "ok", "summary": {"code": "600001", "name": "测试一", "action_bias": "wait", "main_supporting_evidence": ["一号证据"]}, "full": {"stock": {"code": "600001", "name": "测试一"}, "dimension_summary": {"technical": {"verdict": "support", "summary": "一号技术证据"}}, "missing_evidence": []}}),
        _json_response({"stage": "single_stock_deep_dive", "status": "ok", "summary": {"code": "600002", "name": "测试二", "action_bias": "wait", "main_supporting_evidence": ["二号证据"]}, "full": {"stock": {"code": "600002", "name": "测试二"}, "dimension_summary": {"technical": {"verdict": "neutral", "summary": "二号技术证据"}}, "missing_evidence": []}}),
        _json_response({"stage": "single_stock_deep_dive", "status": "ok", "summary": {"code": "600003", "name": "测试三", "action_bias": "wait", "main_supporting_evidence": ["三号证据"]}, "full": {"stock": {"code": "600003", "name": "测试三"}, "dimension_summary": {"technical": {"verdict": "neutral", "summary": "三号技术证据"}}, "missing_evidence": []}}),
        _json_response({"stage": "portfolio_allocation", "status": "ok", "summary": {"portfolio_action": "wait", "core_reason": "等待"}, "full": {"positions_plan": []}}),
        _json_response({"stage": "adversarial_review", "status": "ok", "summary": {"opposing_summary": "等待"}, "full": {"opposing_thesis": {}}}),
        _json_response({"stage": "judge_decision", "status": "ok", "summary": {"primary_plan_verdict": "accept", "final_action": "wait", "decision_summary": "等待", "next_step": "render_final_report"}, "full": {"winner": "mixed"}}),
    ]

    result = run_stock_selection_pipeline(
        task="帮我选一下下周可以入手的股票",
        agent_user_context=_context(),
        tool_registry=registry,
        llm_adapter=adapter,
        run_id="test-rich-report-targets",
        orchestration_mode="legacy",
    )

    assert result.success is True
    assert result.final_report_json["single_stock_deep_dive"]["summary"]["target_count"] == 3
    assert adapter.call_text.call_count == 10
    deep_results = result.final_report_json["single_stock_deep_dive"]["full"]["results"]
    assert [item["summary"]["code"] for item in deep_results] == ["600001", "600002", "600003"]
    assert "600001 测试一" in result.final_markdown
    assert "600002 测试二" in result.final_markdown
    assert "600003 测试三" in result.final_markdown


def test_stock_selection_deep_dive_uses_screening_targets_before_pool_order():
    registry = _registry()
    registry.register(ToolDefinition(
        name="discover_watchlist_candidates",
        description="discover",
        parameters=[],
        handler=lambda market="cn", seed_symbols=None, limit=8: {
            "status": "ok",
            "market": market,
            "candidates": [
                {"code": "688127", "name": "蓝特光学", "market": "cn", "source": "alphasift"},
                {"code": "603629", "name": "利通电子", "market": "cn", "source": "sequoia"},
                {"code": "603256", "name": "宏和科技", "market": "cn", "source": "fundamental"},
                {"code": "688266", "name": "泽璟制药-U", "market": "cn", "source": "fundamental"},
            ][:limit],
        },
    ))
    adapter = MagicMock()
    adapter.call_text.side_effect = [
        _json_response({
            "stage": "candidate_discovery",
            "status": "ok",
            "summary": {"candidate_codes": ["688127", "603629", "603256", "688266"]},
            "full": {"candidates": [{"code": "688127"}, {"code": "603629"}, {"code": "603256"}, {"code": "688266"}]},
        }),
        _json_response({
            "stage": "candidate_screening",
            "status": "ok",
            "summary": {"deep_dive_targets": ["603256", "688266"]},
            "full": {"shortlist": [
                {"code": "688127", "screening_result": "monitor", "score": 100},
                {"code": "603629", "screening_result": "monitor", "score": 100},
                {"code": "603256", "screening_result": "deep_dive", "score": 100},
                {"code": "688266", "screening_result": "deep_dive", "score": 100},
            ]},
        }),
        _json_response({"stage": "single_stock_deep_dive", "status": "ok", "summary": {"code": "603256", "name": "宏和科技", "action_bias": "wait"}, "full": {"stock": {"code": "603256", "name": "宏和科技"}, "missing_evidence": []}}),
        _json_response({"stage": "single_stock_deep_dive", "status": "ok", "summary": {"code": "688266", "name": "泽璟制药-U", "action_bias": "wait"}, "full": {"stock": {"code": "688266", "name": "泽璟制药-U"}, "missing_evidence": []}}),
        _json_response({"stage": "single_stock_deep_dive", "status": "ok", "summary": {"code": "688127", "name": "蓝特光学", "action_bias": "wait"}, "full": {"stock": {"code": "688127", "name": "蓝特光学"}, "missing_evidence": []}}),
        _json_response({"stage": "single_stock_deep_dive", "status": "ok", "summary": {"code": "603629", "name": "利通电子", "action_bias": "wait"}, "full": {"stock": {"code": "603629", "name": "利通电子"}, "missing_evidence": []}}),
        _json_response({"stage": "portfolio_allocation", "status": "ok", "summary": {"portfolio_action": "wait", "core_reason": "等待"}, "full": {"positions_plan": []}}),
        _json_response({"stage": "adversarial_review", "status": "ok", "summary": {"opposing_summary": "等待"}, "full": {"opposing_thesis": {}}}),
        _json_response({"stage": "judge_decision", "status": "ok", "summary": {"primary_plan_verdict": "accept", "final_action": "wait", "decision_summary": "等待", "next_step": "render_final_report"}, "full": {"winner": "mixed"}}),
    ]

    with patch("src.config.Config.get_instance", return_value=type("Cfg", (), {"agent_selection_deep_dive_limit": 4})()):
        result = run_stock_selection_pipeline(
            task="帮我选一下下周可以入手的股票",
            agent_user_context=_context(),
            tool_registry=registry,
            llm_adapter=adapter,
            run_id="test-top-ranked-deep-dive",
            orchestration_mode="legacy",
        )

    deep_results = result.final_report_json["single_stock_deep_dive"]["full"]["results"]
    assert [item["summary"]["code"] for item in deep_results] == ["603256", "688266", "688127", "603629"]


def test_select_deep_dive_targets_tags_pool_fallback_provenance():
    candidates = ["600001", "600002", "600003"]
    screening_summary = {"deep_dive_targets": ["600002"]}
    screening_full = {"shortlist": [{"code": "600002", "screening_result": "deep_dive", "score": 90}]}

    ordered, provenance = _select_deep_dive_targets(
        candidates=candidates,
        screening_summary=screening_summary,
        screening_full=screening_full,
        limit=3,
    )

    assert ordered[0] == "600002"
    assert provenance["600002"] == "screening"
    # 筛选只放行一只，其余按候选池顺序兜底，必须标注 provenance。
    assert provenance["600001"] == "pool_fallback"
    assert provenance["600003"] == "pool_fallback"


def test_candidate_has_desk_tag_detects_desk_fields():
    assert _candidate_has_desk_tag({"primary_desk": "early_turn_desk"}) is True
    assert _candidate_has_desk_tag({"setup_type": "trend_continuation"}) is True
    assert _candidate_has_desk_tag({"desks": ["momentum_desk"]}) is True
    assert _candidate_has_desk_tag({"setup_type": "unknown"}) is False
    assert _candidate_has_desk_tag({"source": "alphasift"}) is False


def test_stock_selection_recommendation_section_always_shows_deep_dived_candidates():
    report = {
        "candidate_discovery": {
            "summary": {"source_count": 4},
            "full": {
                "candidates": [
                    {"code": "688127", "name": "蓝特光学", "source": "alphasift", "signal_score": 100},
                    {"code": "603629", "name": "利通电子", "source": "sequoia", "signal_score": 100},
                    {"code": "603256", "name": "宏和科技", "source": "fundamental", "signal_score": 100},
                    {"code": "688266", "name": "泽璟制药-U", "source": "fundamental", "signal_score": 100},
                ],
            },
        },
        "candidate_screening": {"summary": {}, "full": {}},
        "single_stock_deep_dive": {
            "summary": {},
            "full": {
                "results": [
                    {"summary": {"code": "688127", "name": "蓝特光学", "action_bias": "wait", "action_strength": "weak"}, "full": {"stock": {"code": "688127", "name": "蓝特光学"}, "missing_evidence": []}},
                    {"summary": {"code": "603629", "name": "利通电子", "action_bias": "wait", "action_strength": "weak"}, "full": {"stock": {"code": "603629", "name": "利通电子"}, "missing_evidence": []}},
                    {"summary": {"code": "603256", "name": "宏和科技", "action_bias": "wait", "action_strength": "weak"}, "full": {"stock": {"code": "603256", "name": "宏和科技"}, "missing_evidence": []}},
                    {"summary": {"code": "688266", "name": "泽璟制药-U", "action_bias": "wait", "action_strength": "weak"}, "full": {"stock": {"code": "688266", "name": "泽璟制药-U"}, "missing_evidence": []}},
                ]
            },
        },
        "portfolio_allocation": {"summary": {"portfolio_action": "wait", "core_reason": "等待确认"}, "full": {"positions_plan": []}},
        "adversarial_review": {"summary": {}, "full": {}},
        "judge_decision": {"summary": {"primary_plan_verdict": "accept", "final_action": "wait", "decision_summary": "等待确认"}, "full": {}},
    }

    markdown = render_stock_selection_markdown(report)
    recommendation_section = markdown.split("## 三、关键风险与等待确认", 1)[0]

    assert "## 二、深挖结果与等待/排除决策" in recommendation_section
    assert "### 👀 强观察 1：688127 蓝特光学（候选分 100）" in recommendation_section
    assert "### 👀 强观察 2：603629 利通电子（候选分 100）" in recommendation_section
    assert "### 👀 强观察 3：603256 宏和科技（候选分 100）" in recommendation_section
    assert "### 👀 强观察 4：688266 泽璟制药-U（候选分 100）" in recommendation_section


def test_stock_selection_wait_with_conditions_renders_conditional_entry():
    report = {
        "candidate_discovery": {
            "summary": {"source_count": 1},
            "full": {
                "candidates": [
                    {
                        "code": "688266",
                        "name": "泽璟制药-U",
                        "source": "alphasift:multi_strategy",
                        "signal_score": 96,
                        "reason": "多策略共振。",
                    }
                ]
            },
        },
        "candidate_screening": {"summary": {}, "full": {}},
        "single_stock_deep_dive": {
            "summary": {},
            "full": {
                "results": [
                    {
                        "summary": {
                            "code": "688266",
                            "name": "泽璟制药-U",
                            "action_bias": "wait",
                            "action_strength": "medium",
                            "quote_basis": "after_close",
                            "ideal_entry_zone": "回踩 5 日线不破或放量突破前高",
                            "no_chase_line": "高开超过 6% 且无回踩不追",
                            "stop_loss": "跌破前一日低点",
                            "main_supporting_evidence": ["候选分高且多策略共振"],
                        },
                        "full": {
                            "stock": {"code": "688266", "name": "泽璟制药-U"},
                            "entry_quality": {
                                "auction_trigger": "竞价高开不超过 4% 且承接强",
                                "pullback_trigger": "回踩 5 日线不破",
                                "failure_condition": "跌破前一日低点或板块退潮",
                            },
                            "key_evidence": ["多策略共振"],
                            "failure_conditions": ["跌破前一日低点"],
                            "missing_evidence": [],
                        },
                    }
                ]
            },
        },
        "portfolio_allocation": {
            "summary": {"portfolio_action": "wait", "core_reason": "本轮没有无条件买入标的，但存在可按次日条件触发的强候选"},
            "full": {
                "positions_plan": [
                    {
                        "rank": 1,
                        "code": "688266",
                        "name": "泽璟制药-U",
                        "action": "wait",
                        "action_strength": "medium",
                        "initial_position_pct": 0,
                        "entry_condition": "竞价强承接且开盘 15 分钟不破分时均线",
                        "stop_loss_condition": "跌破前一日低点",
                        "review_trigger": "次日竞价和开盘 15 分钟复查",
                    }
                ]
            },
        },
        "adversarial_review": {"summary": {}, "full": {}},
        "judge_decision": {"summary": {"primary_plan_verdict": "accept_with_changes", "final_action": "wait", "decision_summary": "条件触发后再执行"}, "full": {}},
    }

    markdown = render_stock_selection_markdown(report)
    recommendation_section = markdown.split("## 三、关键风险与等待确认", 1)[0]

    assert "## 二、推荐排序与入场决策" in recommendation_section
    assert "| 机会首选 | 688266 泽璟制药-U |" in markdown
    assert "| 执行首选 | 688266 泽璟制药-U（条件触发） |" in markdown
    assert "### ⚡ 条件入场 1：688266 泽璟制药-U（候选分 96）" in recommendation_section
    assert "| 看盘动作 | 条件入场，不是无条件追买 |" in recommendation_section
    assert "| 必要条件 |" in recommendation_section
    assert "竞价强承接且开盘 15 分钟不破分时均线" in recommendation_section
    assert "| 加分条件 | 竞价强承接且开盘 15 分钟不破分时均线 |" in recommendation_section
    assert "| 加分条件 | 满足其一即可：竞价强承接且开盘 15 分钟不破分时均线 |" not in recommendation_section
    assert "| 可试探仓位 | 5%-10% 试探仓" in recommendation_section
    assert "| 禁止追高 | 高开超过 6% 且无回踩不追 |" in recommendation_section
    assert "| 失效条件 | 跌破前一日低点或板块退潮；跌破前一日低点 |" in recommendation_section


def test_stock_selection_report_uses_chinese_labels_and_split_entry_conditions():
    report = {
        "candidate_discovery": {
            "summary": {"source_count": 1},
            "full": {"candidates": [{"code": "603260", "name": "合盛硅业", "source": "alphasift", "signal_score": 96}]},
        },
        "candidate_screening": {"summary": {}, "full": {}},
        "single_stock_deep_dive": {
            "summary": {},
            "full": {
                "results": [
                    {
                        "summary": {
                            "code": "603260",
                            "name": "合盛硅业",
                            "action_bias": "wait",
                            "action_strength": "medium",
                            "ideal_entry_zone": "价格回踩42.50-43.22区间；成交量萎缩至20日均量80%以下；乖离修复至8%以下；MACD金叉；主力资金净流入",
                            "no_chase_line": "43.50",
                            "stop_loss": "跌破41.50",
                            "main_supporting_evidence": ["资金承接持续"],
                        },
                        "full": {
                            "stock": {"code": "603260", "name": "合盛硅业"},
                            "entry_quality": {
                                "ideal_entry_zone": "价格回踩42.50-43.22区间；成交量萎缩至20日均量80%以下；乖离修复至8%以下；MACD金叉；主力资金净流入",
                                "failure_condition": "跌破41.50",
                            },
                            "key_evidence": ["资金承接持续"],
                            "missing_evidence": [],
                        },
                    }
                ]
            },
        },
        "portfolio_allocation": {
            "summary": {"portfolio_action": "wait", "core_reason": "等待条件触发"},
            "full": {
                "positions_plan": [
                    {
                        "rank": 1,
                        "code": "603260",
                        "name": "合盛硅业",
                        "action": "wait",
                        "action_strength": "medium",
                        "execution_mode": "conditional_open",
                        "initial_position_pct": 0,
                        "entry_condition": "价格回踩42.50-43.22区间；成交量萎缩至20日均量80%以下；乖离修复至8%以下；MACD金叉；主力资金净流入",
                        "stop_loss_condition": "跌破41.50",
                    }
                ]
            },
        },
        "adversarial_review": {"summary": {}, "full": {}},
        "judge_decision": {
            "summary": {
                "primary_plan_verdict": "accept_with_changes",
                "final_action": "wait",
                "decision_summary": "条件触发后再执行",
            },
            "full": {},
        },
    }

    markdown = render_stock_selection_markdown(report)
    main_report = markdown.split("<!-- APPENDIX_SEPARATOR -->", 1)[0]

    assert "| 最终动作 | 等待 |" in main_report
    assert "| 裁决 | 有条件采纳 |" in main_report
    assert "### 字段说明" not in main_report
    assert "Meta-Agent 链路对齐" not in main_report
    assert "Execute 证据摘要" not in main_report
    assert "| 必要条件 | 价格回踩42.50-43.22区间；成交量萎缩至20日均量80%以下 |" in main_report
    assert "| 加分条件 | 满足其一即可：MACD金叉 / 主力资金净流入 |" in main_report


def test_stock_selection_headline_and_meta_chain_align_with_deep_dive_body():
    report = {
        "candidate_discovery": {
            "summary": {"source_count": 3},
            "full": {
                "candidates": [
                    {"code": "300308", "name": "中际旭创", "source": "news_theme_daily", "final_score": 98, "reason": "CPO 主题催化"},
                    {"code": "600487", "name": "亨通光电", "source": "news_theme_daily", "final_score": 95, "reason": "光通信主题催化"},
                    {"code": "300628", "name": "亿联网络", "source": "capital_flow", "final_score": 92, "reason": "资金流入但未深挖"},
                ]
            },
        },
        "candidate_screening": {"summary": {}, "full": {}},
        "single_stock_deep_dive": {
            "summary": {},
            "full": {
                "results": [
                    {
                        "summary": {
                            "code": "300308",
                            "name": "中际旭创",
                            "action_bias": "wait",
                            "action_strength": "medium",
                            "ideal_entry_zone": "回踩 MA20 企稳",
                            "stop_loss": "跌破 MA20",
                            "main_supporting_evidence": ["资金与主题共振"],
                        },
                        "full": {
                            "stock": {"code": "300308", "name": "中际旭创"},
                            "entry_quality": {"failure_condition": "跌破 MA20"},
                            "key_evidence": ["资金与主题共振"],
                            "failure_conditions": ["跌破 MA20"],
                        },
                    },
                    {
                        "summary": {
                            "code": "600487",
                            "name": "亨通光电",
                            "action_bias": "wait",
                            "action_strength": "medium",
                            "ideal_entry_zone": "回踩支撑区企稳",
                            "stop_loss": "跌破支撑区",
                            "main_supporting_evidence": ["资金流入持续"],
                        },
                        "full": {
                            "stock": {"code": "600487", "name": "亨通光电"},
                            "entry_quality": {"failure_condition": "跌破支撑区"},
                            "key_evidence": ["资金流入持续"],
                            "failure_conditions": ["跌破支撑区"],
                        },
                    },
                ]
            },
        },
        "meta_orchestrator": {
            "summary": {"package_count": 2},
            "full": {
                "packages": [
                    {"stock": {"code": "300308", "name": "中际旭创"}, "meta_analysis": {"asset_regime": "Right_Side_Momentum"}},
                    {"stock": {"code": "600487", "name": "亨通光电"}, "meta_analysis": {"asset_regime": "Right_Side_Momentum"}},
                    {"stock": {"code": "603341", "name": "龙旗科技"}, "meta_analysis": {"asset_regime": "Event_Theme_Watch"}},
                ]
            },
        },
        "pricing_agent": {
            "summary": {"priced_count": 3},
            "full": {
                "if_then_order_matrix": [
                    {"code": "300308", "name": "中际旭创", "selected_scenario": "Mean_Reversion_Pullback"},
                    {"code": "600487", "name": "亨通光电", "selected_scenario": "Mean_Reversion_Pullback"},
                    {"code": "603341", "name": "龙旗科技", "selected_scenario": "Watch_Only"},
                ]
            },
        },
        "portfolio_allocation": {
            "summary": {
                "portfolio_action": "wait",
                "core_reason": "本轮没有无条件买入标的，但存在可按次日条件触发的强候选",
            },
            "full": {
                "positions_plan": [
                    {"rank": 1, "code": "300308", "name": "中际旭创", "action": "wait", "execution_mode": "conditional_open", "entry_condition": "回踩 MA20 企稳", "stop_loss_condition": "跌破 MA20"},
                    {"rank": 2, "code": "600487", "name": "亨通光电", "action": "wait", "execution_mode": "conditional_open", "entry_condition": "回踩支撑区企稳", "stop_loss_condition": "跌破支撑区"},
                ]
            },
        },
        "adversarial_review": {"summary": {}, "full": {}},
        "judge_decision": {"summary": {"primary_plan_verdict": "wait_for_more_data", "final_action": "wait"}, "full": {}},
    }

    markdown = render_stock_selection_markdown(report)
    observation_pool = markdown.split("### 观察池", 1)[1].split("## 三、关键风险与等待确认", 1)[0]

    assert "| 机会首选 | 300308 中际旭创 |" in markdown
    assert "| 执行首选 | 300308 中际旭创（条件触发） |" in markdown
    assert "| 可观察标的 | 600487 亨通光电 |" in markdown
    assert "| 可观察标的 | 300628 亿联网络 |" not in markdown
    assert "300628 亿联网络" in observation_pool
    assert "## 附录一、系统推理链路（调试用）" in markdown
    assert "### 300308 中际旭创" in markdown
    assert "### 600487 亨通光电" in markdown
    assert "### 603341 龙旗科技" not in markdown
    assert "### 1. 300308 中际旭创" not in markdown


def test_stock_selection_headline_watch_excludes_opportunity_and_execution_picks():
    report = {
        "candidate_discovery": {
            "summary": {"source_count": 3},
            "full": {
                "candidates": [
                    {"code": "688401", "name": "路维光电", "source": "theme_catalyst", "final_score": 94, "reason": "光刻掩膜版主题催化"},
                    {"code": "300308", "name": "中际旭创", "source": "momentum", "final_score": 93, "reason": "CPO 动量延续"},
                    {"code": "600487", "name": "亨通光电", "source": "capital_flow", "final_score": 88, "reason": "资金流入但等待确认"},
                ]
            },
        },
        "candidate_screening": {"summary": {}, "full": {}},
        "single_stock_deep_dive": {
            "summary": {},
            "full": {
                "results": [
                    {
                        "summary": {
                            "code": "688401",
                            "name": "路维光电",
                            "action_bias": "wait",
                            "action_strength": "medium",
                            "ideal_entry_zone": "回踩 5 日线企稳",
                            "stop_loss": "跌破前低",
                            "main_supporting_evidence": ["主题与基本面匹配，机会质量最高"],
                        },
                        "full": {
                            "stock": {"code": "688401", "name": "路维光电"},
                            "key_evidence": ["主题与基本面匹配，机会质量最高"],
                            "failure_conditions": ["跌破前低"],
                        },
                    },
                    {
                        "summary": {
                            "code": "300308",
                            "name": "中际旭创",
                            "action_bias": "wait",
                            "action_strength": "medium",
                            "ideal_entry_zone": "回踩 MA20 企稳",
                            "stop_loss": "跌破 MA20",
                            "main_supporting_evidence": ["动量和资金共振"],
                        },
                        "full": {
                            "stock": {"code": "300308", "name": "中际旭创"},
                            "key_evidence": ["动量和资金共振"],
                            "failure_conditions": ["跌破 MA20"],
                        },
                    },
                    {
                        "summary": {
                            "code": "600487",
                            "name": "亨通光电",
                            "action_bias": "wait",
                            "action_strength": "medium",
                            "ideal_entry_zone": "回踩支撑确认",
                            "stop_loss": "跌破支撑",
                            "main_supporting_evidence": ["资金流入，仍需确认"],
                        },
                        "full": {
                            "stock": {"code": "600487", "name": "亨通光电"},
                            "key_evidence": ["资金流入，仍需确认"],
                            "failure_conditions": ["跌破支撑"],
                        },
                    },
                ]
            },
        },
        "portfolio_allocation": {
            "summary": {
                "portfolio_action": "wait",
                "core_reason": "本轮没有无条件买入标的，但存在可按次日条件触发的强候选",
            },
            "full": {
                "positions_plan": [
                    {
                        "rank": 1,
                        "code": "300308",
                        "name": "中际旭创",
                        "action": "wait",
                        "execution_mode": "conditional_open",
                        "entry_condition": "回踩 MA20 企稳",
                        "stop_loss_condition": "跌破 MA20",
                    },
                    {
                        "rank": 2,
                        "code": "688401",
                        "name": "路维光电",
                        "action": "wait",
                        "action_strength": "medium",
                        "execution_mode": "strong_watch",
                        "entry_condition": "等待量价确认",
                        "stop_loss_condition": "跌破前低",
                    },
                ]
            },
        },
        "adversarial_review": {"summary": {}, "full": {}},
        "judge_decision": {"summary": {"primary_plan_verdict": "wait_for_more_data", "final_action": "wait"}, "full": {}},
    }

    markdown = render_stock_selection_markdown(report)
    headline = markdown.split("## 二、", 1)[0]

    assert "| 机会首选 | 688401 路维光电 |" in headline
    assert "| 执行首选 | 300308 中际旭创（条件触发） |" in headline
    assert "| 可观察标的 | 600487 亨通光电 |" in headline
    assert "| 可观察标的 | 688401 路维光电 |" not in headline
    assert "| 可观察标的 | 300308 中际旭创 |" not in headline


def test_stock_selection_high_score_without_exit_condition_renders_strong_watch():
    report = {
        "candidate_discovery": {
            "summary": {"source_count": 1},
            "full": {"candidates": [{"code": "688127", "name": "蓝特光学", "source": "sequoia:multi_strategy", "signal_score": 92}]},
        },
        "candidate_screening": {"summary": {}, "full": {}},
        "single_stock_deep_dive": {
            "summary": {},
            "full": {
                "results": [
                    {
                        "summary": {
                            "code": "688127",
                            "name": "蓝特光学",
                            "action_bias": "wait",
                            "action_strength": "medium",
                            "ideal_entry_zone": "放量突破后观察",
                            "main_missing_evidence": ["缺少止损条件"],
                        },
                        "full": {
                            "stock": {"code": "688127", "name": "蓝特光学"},
                            "missing_evidence": ["缺少止损条件"],
                            "risk_flags": [],
                        },
                    }
                ]
            },
        },
        "portfolio_allocation": {"summary": {"portfolio_action": "wait", "core_reason": "等待"}, "full": {"positions_plan": []}},
        "adversarial_review": {"summary": {}, "full": {}},
        "judge_decision": {"summary": {"primary_plan_verdict": "accept", "final_action": "wait", "decision_summary": "等待确认"}, "full": {}},
    }

    markdown = render_stock_selection_markdown(report)
    recommendation_section = markdown.split("## 四、Execute 证据摘要", 1)[0]

    assert "### 👀 强观察 1：688127 蓝特光学（候选分 92）" in recommendation_section
    assert "| 看盘动作 | 强观察，暂不形成交易脚本 |" in recommendation_section
    assert "| 不能直接入场原因 | 缺少止损条件 |" in recommendation_section
    assert "⚡ 条件入场" not in recommendation_section


def test_stock_selection_balanced_candidates_skip_duplicates_and_fill_next_items():
    candidates = [
        {"code": "600001", "name": "策略一", "source": "alphasift:multi_strategy", "signal_score": 98},
        {"code": "600002", "name": "策略二", "source": "sequoia:multi_strategy", "signal_score": 97},
        {"code": "600001", "name": "消息重复一", "source": "news_momentum:company_event", "signal_score": 99},
        {"code": "600002", "name": "消息重复二", "source": "event_impact:policy", "signal_score": 96},
        {"code": "600003", "name": "消息三", "source": "news_momentum:company_event", "signal_score": 95},
        {"code": "600004", "name": "消息四", "source": "event_impact:policy", "signal_score": 94},
        {"code": "600003", "name": "资金重复三", "source": "capital:main_flow", "signal_score": 93},
        {"code": "600005", "name": "资金五", "source": "capital:main_flow", "signal_score": 92},
        {"code": "600006", "name": "资金六", "source": "moneyflow:hot", "signal_score": 91},
        {"code": "600007", "name": "基本面七", "source": "fundamental:quality_snapshot", "signal_score": 90},
        {"code": "600008", "name": "基本面八", "source": "fundamental:quality_snapshot", "signal_score": 89},
    ]
    registry = _registry_with_candidates(candidates)
    adapter = MagicMock()
    adapter.call_text.side_effect = [
        _json_response({"stage": "candidate_discovery", "status": "ok", "summary": {"candidate_codes": [item["code"] for item in candidates]}, "full": {"candidates": candidates}}),
        _json_response({"stage": "candidate_screening", "status": "ok", "summary": {"deep_dive_targets": ["600001"]}, "full": {"shortlist": []}}),
        *[_deep_dive_response(code) for code in ["600001", "600002", "600003", "600004", "600005", "600006", "600007", "600008"]],
        _json_response({"stage": "portfolio_allocation", "status": "ok", "summary": {"portfolio_action": "wait", "core_reason": "等待"}, "full": {"positions_plan": []}}),
        _json_response({"stage": "adversarial_review", "status": "ok", "summary": {"opposing_summary": "等待"}, "full": {"opposing_thesis": {}}}),
        _json_response({"stage": "judge_decision", "status": "ok", "summary": {"primary_plan_verdict": "accept", "final_action": "wait", "decision_summary": "等待", "next_step": "render_final_report"}, "full": {"winner": "mixed"}}),
    ]

    result = run_stock_selection_pipeline(
        task="帮我选一下下周可以入手的股票",
        agent_user_context=_context(),
        tool_registry=registry,
        llm_adapter=adapter,
        run_id="test-balanced-dedupe-fill",
        orchestration_mode="legacy",
    )

    summary = result.final_report_json["balanced_candidate_evidence"]["summary"]
    assert summary["targets"] == ["600001", "600002", "600003", "600004", "600005", "600006", "600007", "600008"]
    assert summary["bucket_counts"] == {"strategy": 2, "news": 2, "capital": 2, "fundamental": 2}


def test_stock_selection_balanced_candidate_evidence_collects_candidates_in_parallel():
    candidates = [
        {"code": "600001", "name": "测试一", "market": "cn", "source": "alphasift:multi_strategy"},
        {"code": "600002", "name": "测试二", "market": "cn", "source": "sequoia:multi_strategy"},
        {"code": "600003", "name": "测试三", "market": "cn", "source": "news_momentum:company_event"},
        {"code": "600004", "name": "测试四", "market": "cn", "source": "capital:main_flow"},
    ]
    registry = _registry_with_candidates(candidates)
    registry.register(ToolDefinition(
        name="analyze_trend",
        description="trend",
        parameters=[ToolParameter(name="stock_code", type="string", description="code")],
        handler=lambda stock_code: time.sleep(0.08) or {"code": stock_code, "trend_status": "多头排列"},
    ))
    adapter = MagicMock()
    adapter.call_text.side_effect = [
        _json_response({"stage": "candidate_discovery", "status": "ok", "summary": {"candidate_codes": [item["code"] for item in candidates]}, "full": {"candidates": candidates}}),
        _json_response({"stage": "candidate_screening", "status": "ok", "summary": {"deep_dive_targets": ["600001"]}, "full": {"shortlist": []}}),
        *[_deep_dive_response(code) for code in ["600001", "600002", "600003", "600004"]],
        _json_response({"stage": "portfolio_allocation", "status": "ok", "summary": {"portfolio_action": "wait", "core_reason": "等待"}, "full": {"positions_plan": []}}),
        _json_response({"stage": "adversarial_review", "status": "ok", "summary": {"opposing_summary": "等待"}, "full": {"opposing_thesis": {}}}),
        _json_response({"stage": "judge_decision", "status": "ok", "summary": {"primary_plan_verdict": "accept", "final_action": "wait", "decision_summary": "等待", "next_step": "render_final_report"}, "full": {"winner": "mixed"}}),
    ]

    started = time.monotonic()
    result = run_stock_selection_pipeline(
        task="帮我选一下下周可以入手的股票",
        agent_user_context=_context(),
        tool_registry=registry,
        llm_adapter=adapter,
        run_id="test-balanced-parallel",
        orchestration_mode="legacy",
    )
    elapsed = time.monotonic() - started

    assert result.success is True
    assert result.final_report_json["balanced_candidate_evidence"]["summary"]["target_count"] == 4
    assert elapsed < 0.28


def test_compact_candidate_seed_preserves_expert_packets_for_trace_ui():
    seed = {
        "status": "ok",
        "market": "cn",
        "candidate_source": "expert_graph_discovery",
        "candidate_count": 2,
        "candidates": [
            {"code": "600001", "name": "测试一", "source": "multi_expert_recall", "signal_score": 92.5},
            {"code": "600002", "name": "测试二", "source": "sequoia:multi_strategy", "signal_score": 88.0},
        ],
        "expert_packets": [
            {
                "expert": "strategy_factor_expert",
                "dimension": "strategy",
                "status": "ok",
                "data_quality": {"freshness": "daily"},
                "candidates": [
                    {"code": "600001", "name": "测试一", "source": "alphasift:balanced_alpha", "reason": "策略候选", "score": 91, "confidence": 0.7},
                ],
                "themes": [],
                "diagnostics": [],
                "errors": [],
            },
            {
                "expert": "technical_candidate_expert",
                "dimension": "technical",
                "status": "ok",
                "data_quality": {"freshness": "daily"},
                "candidates": [
                    {"code": "600002", "name": "测试二", "source": "sequoia:turtle_trade", "reason": "技术候选", "score": 89, "confidence": 0.68},
                ],
                "themes": [],
                "diagnostics": [],
                "errors": [],
            },
        ],
        "discovery_steps": [],
        "errors": [],
    }

    compact = _compact_candidate_seed(seed, limit=8)

    assert len(compact["expert_packets"]) == 2
    assert compact["expert_packets"][0]["expert"] == "strategy_factor_expert"
    assert compact["expert_packets"][0]["candidates"][0]["code"] == "600001"
    assert compact["expert_packets"][1]["expert"] == "technical_candidate_expert"
    assert compact["expert_packets"][1]["candidates"][0]["code"] == "600002"


def test_stock_selection_applies_judge_monitor_override_to_portfolio_plan():
    adapter = MagicMock()
    adapter.call_text.side_effect = [
        _json_response({"stage": "candidate_discovery", "status": "ok", "summary": {"candidate_codes": ["300572"]}, "full": {"candidates": [{"code": "300572", "name": "安车检测"}]}}),
        _json_response({"stage": "candidate_screening", "status": "ok", "summary": {"deep_dive_targets": ["300572"]}, "full": {"shortlist": [{"code": "300572", "name": "安车检测", "screening_result": "deep_dive"}]}}),
        _json_response({"stage": "single_stock_deep_dive", "status": "ok", "summary": {"code": "300572", "name": "安车检测", "action_bias": "wait", "action_strength": "weak"}, "full": {"stock": {"code": "300572", "name": "安车检测"}, "missing_evidence": ["fundamental"]}}),
        _json_response({"stage": "meta_orchestrator", "status": "partial", "summary": {"package_count": 1, "asset_regimes": [{"code": "300572", "asset_regime": "Unknown"}], "market_context_note": "基本面缺口", "main_constraints": []}, "full": {"packages": [{"stock": {"code": "300572", "name": "安车检测", "market": "cn"}, "meta_analysis": {"asset_regime": "Unknown"}, "hard_constraints_for_pricing_agent": {"risk_constraints": [{"constraint": "fundamental missing", "source": "deep_dive"}]}, "required_pricing_scenarios": []}]}}),
        _json_response({"stage": "pricing_agent", "status": "partial", "summary": {"priced_count": 1, "tradable_count": 0, "main_pricing_constraints": ["fundamental missing"], "pricing_note": "仅监控"}, "full": {"if_then_order_matrix": [{"code": "300572", "name": "安车检测", "asset_regime": "Unknown", "selected_scenario": "Mean_Reversion_Pullback", "scenarios": [{"scenario_name": "Mean_Reversion_Pullback", "condition": "基本面补齐后复查", "action": "monitor", "execution_mode": "plain_wait", "entry_zone": "等待", "stop_loss": "跌破 30", "failure_condition": "基本面证据继续缺失", "risk_reward_comment": "仅监控", "constraints_used": ["fundamental missing"]}]}]}}),
        _json_response({
            "stage": "portfolio_allocation",
            "status": "ok",
            "summary": {
                "portfolio_action": "wait",
                "core_reason": "等待回踩",
                "positions_plan_brief": [{"code": "300572", "action": "wait", "initial_position_pct": 0}],
            },
            "full": {
                "positions_plan": [{
                    "rank": 1,
                    "code": "300572",
                    "name": "安车检测",
                    "action": "wait",
                    "action_strength": "weak",
                    "initial_position_pct": 0,
                    "entry_condition": "回踩到 33 元可以买入",
                    "stop_loss_condition": "跌破 30",
                    "review_trigger": "下周一",
                }]
            },
        }),
        _json_response({"stage": "adversarial_review", "status": "ok", "summary": {"opposing_summary": "基本面缺口"}, "full": {"opposing_thesis": {}}}),
        _json_response({
            "stage": "judge_decision",
            "status": "ok",
            "summary": {
                "primary_plan_verdict": "accept_with_changes",
                "final_action": "wait",
                "decision_summary": "将300572从等待降为仅监控（monitor），取消具体触发条件。",
                "next_step": "render_final_report",
            },
            "full": {"winner": "opposing", "required_plan_changes": ["将300572从等待降为仅监控（monitor）"]},
        }),
    ]

    result = run_stock_selection_pipeline(
        task="帮我选一下下周可以入手的股票",
        agent_user_context=_context(),
        tool_registry=_registry_with_candidates([{"code": "300572", "name": "安车检测", "market": "cn", "source": "test"}]),
        llm_adapter=adapter,
        run_id="test-monitor-override",
        orchestration_mode="legacy",
    )

    plan = result.final_report_json["portfolio_allocation"]["full"]["positions_plan"][0]
    assert plan["action"] == "monitor"
    assert plan["initial_position_pct"] == 0
    assert "仅监控" in plan["entry_condition"]
    assert "回踩到 33 元可以买入" not in result.final_markdown
    assert result.final_report_json["portfolio_allocation"]["summary"]["positions_plan_brief"][0]["action"] == "monitor"


def test_stock_selection_deep_dive_fills_missing_entry_fields_from_fallback_template():
    adapter = MagicMock()
    adapter.call_text.side_effect = [
        _json_response({"stage": "candidate_discovery", "status": "ok", "summary": {"candidate_codes": ["688127"]}, "full": {"candidates": [{"code": "688127", "name": "蓝特光学", "signal_score": 100}]}}),
        _json_response({"stage": "candidate_screening", "status": "ok", "summary": {"deep_dive_targets": ["688127"]}, "full": {"shortlist": [{"code": "688127", "name": "蓝特光学", "screening_result": "deep_dive"}]}}),
        _json_response({
            "stage": "single_stock_deep_dive",
            "status": "ok",
            "summary": {
                "code": "688127",
                "name": "蓝特光学",
                "action_bias": "wait",
                "action_strength": "weak",
                "quote_basis": "intraday",
                "main_supporting_evidence": ["技术趋势信号：分析显示强势多头。"],
                "main_risks": ["高获利盘比例，存在兑现压力。"],
                "main_missing_evidence": ["基本面分析缺失"],
            },
            "full": {
                "stock": {"code": "688127", "name": "蓝特光学", "market": "cn", "data_status": "ok"},
                "action_bias": "wait",
                "action_strength": "weak",
                "quote_basis": "intraday",
                "dimension_summary": {
                    "technical": {"verdict": "support", "summary": "强势多头"},
                    "capital_flow": {"verdict": "support", "summary": "主力资金近期净流入"},
                },
                "key_evidence": ["技术趋势信号：分析显示强势多头。"],
                "risk_flags": ["高获利盘比例，存在兑现压力。"],
                "failure_conditions": ["跌破关键支撑"],
                "missing_evidence": ["基本面分析缺失"],
            },
        }),
        _json_response({"stage": "portfolio_allocation", "status": "ok", "summary": {"portfolio_action": "wait", "core_reason": "等待"}, "full": {"positions_plan": []}}),
        _json_response({"stage": "adversarial_review", "status": "ok", "summary": {"opposing_summary": "等待"}, "full": {"opposing_thesis": {}}}),
        _json_response({"stage": "judge_decision", "status": "ok", "summary": {"primary_plan_verdict": "accept", "final_action": "wait", "decision_summary": "等待", "next_step": "render_final_report"}, "full": {"winner": "mixed"}}),
    ]

    result = run_stock_selection_pipeline(
        task="分析 688127",
        agent_user_context=_context(),
        tool_registry=_registry_with_candidates([{"code": "688127", "name": "蓝特光学", "market": "cn", "source": "alphasift"}]),
        llm_adapter=adapter,
        run_id="test-deep-dive-normalize-entry-fields",
        orchestration_mode="legacy",
    )

    deep_full = result.final_report_json["single_stock_deep_dive"]["full"]["results"][0]["full"]
    entry_quality = deep_full["entry_quality"]
    assert entry_quality["ideal_entry_zone"] == "等待回踩或突破确认"
    assert entry_quality["secondary_entry_zone"] == "突破后回踩不破"
    assert entry_quality["no_chase_line"] == "高于关键压力位且乖离扩大时不追"
    assert entry_quality["stop_loss"] == "跌破关键支撑或账户止损线"
    assert entry_quality["target_1"] == "前高或筹码压力位"
    assert entry_quality["target_2"] == "趋势延伸位"


def test_expert_state_schema_serializes_for_trace():
    state = AgentState(
        task="选股",
        market="cn",
        orchestration_mode="expert_graph",
        evidence_bundle=EvidenceBundle(candidate_pool=[{"code": "600001", "name": "测试一"}]),
    )
    state.add_opinion(ExpertOpinion(
        expert_name="technical_expert",
        dimension="technical",
        verdict="support",
        confidence=0.8,
        summary="技术面支持",
    ))

    payload = state.to_trace_dict()

    assert payload["orchestration_mode"] == "expert_graph"
    assert payload["evidence_bundle"]["candidate_pool"][0]["name"] == "测试一"
    assert payload["expert_opinions"]["technical_expert"]["verdict"] == "support"


def test_stock_selection_expert_graph_adds_expert_opinions():
    adapter = MagicMock()
    adapter.call_text.side_effect = [
        _json_response({
            "stage": "candidate_discovery",
            "status": "ok",
            "summary": {"candidate_codes": ["600001"], "main_limitations": []},
            "full": {
                "candidates": [
                    {
                        "code": "600001",
                        "name": "测试一",
                        "source": "alphasift:capital_heat",
                        "strategy_tags": ["capital_heat", "volume_breakout"],
                        "reason_dimensions": [
                            {"dimension": "strategy", "label": "策略", "detail": "AlphaSift YAML 多因子策略入池：资金热度"},
                            {"dimension": "capital", "label": "资金面", "detail": "成交额=10亿"},
                        ],
                    }
                ],
                "missing_evidence": [],
            },
        }),
        _json_response({
            "stage": "candidate_screening",
            "status": "ok",
            "summary": {"deep_dive_targets": ["600001"], "monitor_targets": [], "rejected_targets": [], "main_limitations": []},
            "full": {"shortlist": [{"code": "600001", "name": "测试一", "screening_result": "deep_dive", "score": 80}]},
        }),
        _json_response({
            "stage": "single_stock_deep_dive",
            "status": "ok",
            "summary": {"code": "600001", "name": "测试一", "action_bias": "wait", "action_strength": "weak"},
            "full": {
                "stock": {"code": "600001", "name": "测试一"},
                "dimension_summary": {
                    "technical": {"verdict": "support", "summary": "多头排列"},
                    "capital_flow": {"verdict": "tool_failed", "summary": "资金流失败"},
                    "fundamental": {"verdict": "neutral", "summary": "基本面中性"},
                    "news_event": {"verdict": "neutral", "summary": "无重大利空"},
                },
                "missing_evidence": ["capital_flow"],
                "tool_failures": [{"tool": "get_capital_flow", "error": ["timeout"]}],
            },
        }),
        _json_response({
            "stage": "portfolio_allocation",
            "status": "ok",
            "summary": {"portfolio_action": "wait", "recommended_position_count": 0, "initial_total_position_pct": 0, "core_reason": "等待资金确认"},
            "full": {"positions_plan": [], "risk_controls": ["资金流失败时不追高"]},
        }),
        _json_response({
            "stage": "adversarial_review",
            "status": "ok",
            "summary": {"opposing_summary": "资金流缺失，不应追高"},
            "full": {"opposing_thesis": {}},
        }),
        _json_response({
            "stage": "judge_decision",
            "status": "ok",
            "summary": {"primary_plan_verdict": "accept_with_changes", "final_action": "wait", "decision_summary": "等待确认", "next_step": "render_final_report"},
            "full": {"winner": "mixed"},
        }),
    ]

    result = run_stock_selection_pipeline(
        task="我现在有5w元，帮我选股",
        agent_user_context=_context(),
        tool_registry=_registry(),
        llm_adapter=adapter,
        run_id="test-expert-graph",
        orchestration_mode="expert_graph",
    )

    expert_state = result.final_report_json["expert_state"]
    assert result.success is True
    assert result.final_report_json["orchestration_mode"] == "expert_graph"
    assert expert_state["orchestration_mode"] == "expert_graph"
    assert expert_state["evidence_bundle"]["evidence_cards"]
    assert expert_state["evidence_bundle"]["expert_packets"]
    assert expert_state["evidence_bundle"]["judge_input_packet"]["decision_matrix"]
    assert "technical_expert" in expert_state["expert_opinions"]
    assert "capital_chip_expert" in expert_state["expert_opinions"]
    assert expert_state["expert_opinions"]["candidate_discovery_expert"]["candidate_impacts"][0]["name"] == "测试一"
    deep_cards = result.final_report_json["single_stock_deep_dive"]["full"]["results"][0]["full"]["evidence_cards"]
    assert any(card["dimension"] == "capital_flow" for card in deep_cards)
    assert result.context.expert_state is not None


def test_stock_selection_expert_graph_runs_when_candidate_pool_empty():
    registry = _registry()
    registry.register(ToolDefinition(
        name="discover_watchlist_candidates",
        description="discover",
        parameters=[],
        handler=lambda market="cn", seed_symbols=None, limit=8: {
            "status": "ok",
            "market": market,
            "candidates": [],
        },
    ))
    adapter = MagicMock()
    adapter.call_text.side_effect = [
        _json_response({
            "stage": "candidate_discovery",
            "status": "ok",
            "summary": {"candidate_codes": [], "main_limitations": ["候选池为空"]},
            "full": {"candidates": [], "missing_evidence": ["discover_watchlist_candidates"]},
        }),
    ]

    result = run_stock_selection_pipeline(
        task="我现在有5w元，帮我选股",
        agent_user_context=_context(),
        tool_registry=registry,
        llm_adapter=adapter,
        run_id="test-empty-expert-graph",
        orchestration_mode="expert_graph",
    )

    expert_state = result.final_report_json["expert_state"]
    assert result.success is True
    assert result.final_report_json["orchestration_mode"] == "expert_graph"
    assert expert_state["orchestration_mode"] == "expert_graph"
    assert expert_state["expert_opinions"]["candidate_discovery_expert"]["verdict"] == "insufficient_data"
    assert result.context.expert_state is not None


def test_executor_uses_stock_selection_for_watchlist_scan():
    adapter = MagicMock()
    adapter.call_text.side_effect = [
        _json_response({"stage": "candidate_discovery", "status": "ok", "summary": {"candidate_codes": ["600001"]}, "full": {"candidates": [{"code": "600001", "name": "测试一"}]}}),
        _json_response({"stage": "candidate_screening", "status": "ok", "summary": {"deep_dive_targets": ["600001"]}, "full": {"shortlist": []}}),
        _json_response({"stage": "single_stock_deep_dive", "status": "ok", "summary": {"code": "600001", "name": "测试一", "action_bias": "wait"}, "full": {"stock": {"code": "600001", "name": "测试一"}, "missing_evidence": []}}),
        _json_response({"stage": "portfolio_allocation", "status": "ok", "summary": {"portfolio_action": "wait", "core_reason": "等待"}, "full": {"positions_plan": []}}),
        _json_response({"stage": "adversarial_review", "status": "ok", "summary": {"opposing_summary": "等待"}, "full": {"opposing_thesis": {}}}),
        _json_response({"stage": "judge_decision", "status": "ok", "summary": {"primary_plan_verdict": "accept", "final_action": "wait", "decision_summary": "等待", "next_step": "render_final_report"}, "full": {"winner": "mixed"}}),
    ]

    executor = AgentExecutor(_registry(), adapter, max_steps=1)
    result = executor._run_loop(
        messages=[],
        tool_decls=[],
        parse_dashboard=False,
        original_task="我现在有5w元，你帮我选股",
        context={"agent_user_context": _context()},
    )

    assert result.success is True
    assert result.stock_selection["success"] is True
    assert "选股分析报告：下周可关注候选" in result.content
    assert not adapter.call_with_tools.called


def test_multi_agent_orchestrator_uses_expert_graph_stock_selection_for_watchlist_scan():
    adapter = MagicMock()
    adapter.call_text.side_effect = [
        _json_response({"stage": "candidate_discovery", "status": "ok", "summary": {"candidate_codes": ["600001"]}, "full": {"candidates": [{"code": "600001", "name": "测试一"}]}}),
        _json_response({"stage": "candidate_screening", "status": "ok", "summary": {"deep_dive_targets": ["600001"]}, "full": {"shortlist": []}}),
        _json_response({"stage": "single_stock_deep_dive", "status": "ok", "summary": {"code": "600001", "name": "测试一", "action_bias": "wait"}, "full": {"stock": {"code": "600001", "name": "测试一"}, "missing_evidence": []}}),
        _json_response({"stage": "portfolio_allocation", "status": "ok", "summary": {"portfolio_action": "wait", "core_reason": "等待"}, "full": {"positions_plan": []}}),
        _json_response({"stage": "adversarial_review", "status": "ok", "summary": {"opposing_summary": "等待"}, "full": {"opposing_thesis": {}}}),
        _json_response({"stage": "judge_decision", "status": "ok", "summary": {"primary_plan_verdict": "accept", "final_action": "wait", "decision_summary": "等待", "next_step": "render_final_report"}, "full": {"winner": "mixed"}}),
    ]
    config = MagicMock()
    config.agent_orchestration_mode = "expert_graph"
    config.agent_orchestrator_timeout_s = 0
    orchestrator = AgentOrchestrator(
        tool_registry=_registry_with_candidates([{"code": "600001", "name": "测试一", "market": "cn", "source": "test"}]),
        llm_adapter=adapter,
        config=config,
    )
    events = []

    result = orchestrator.chat(
        "我现在有5w元，你帮我选股",
        session_id="test-multi-expert-selection",
        context={"agent_user_context": _context()},
        progress_callback=events.append,
    )

    final_report = result.stock_selection["final_report_json"]
    assert result.success is True
    assert final_report["orchestration_mode"] == "expert_graph"
    assert final_report["expert_state"]["orchestration_mode"] == "expert_graph"
    assert "selection_expert_graph_done" in [event["type"] for event in events]
    expert_event = next(event for event in events if event["type"] == "selection_expert_graph_done")
    assert expert_event["payload"]["expert_state"]["orchestration_mode"] == "expert_graph"
    assert "candidate_discovery_expert" in expert_event["payload"]["expert_state"]["expert_opinions"]
    assert not adapter.call_with_tools.called


def test_stock_selection_regime_risk_off_downgrades_open_plan():
    registry = _registry_with_candidates([{"code": "600001", "name": "测试一", "market": "cn", "source": "test"}])
    registry.register(ToolDefinition(
        name="detect_market_regime",
        description="regime",
        parameters=[],
        handler=lambda market="cn", persist=True: {
            "status": "ok",
            "market": market,
            "regime": "risk_off",
            "risk_level": "high",
            "volatility_bucket": "extreme",
            "sentiment_state": "fear",
            "wyckoff_phase": "markdown",
            "risk_multiplier": 1.8,
            "strategy_hints": ["risk_off 下优先降低风险暴露，开仓需等待市场确认。"],
            "data_quality": "limited",
        },
    ))
    adapter = MagicMock()
    adapter.call_text.side_effect = [
        _json_response({"stage": "candidate_discovery", "status": "ok", "summary": {"candidate_codes": ["600001"]}, "full": {"candidates": [{"code": "600001", "name": "测试一"}]}}),
        _json_response({"stage": "candidate_screening", "status": "ok", "summary": {"deep_dive_targets": ["600001"]}, "full": {"shortlist": [{"code": "600001", "name": "测试一", "screening_result": "deep_dive", "score": 80, "score_breakdown": {}}]}}),
        _json_response({"stage": "single_stock_deep_dive", "status": "ok", "summary": {"code": "600001", "name": "测试一", "action_bias": "open", "action_strength": "medium"}, "full": {"stock": {"code": "600001", "name": "测试一"}, "action_bias": "open", "missing_evidence": []}}),
        _json_response({
            "stage": "portfolio_allocation",
            "status": "ok",
            "summary": {"portfolio_action": "open", "recommended_position_count": 1, "initial_total_position_pct": 10, "reserved_cash_pct": 90, "core_reason": "模型认为可开仓", "main_constraint": ""},
            "full": {"positions_plan": [{"rank": 1, "code": "600001", "name": "测试一", "action": "open", "action_strength": "medium", "initial_position_pct": 10, "initial_amount": 5000, "entry_condition": "突破", "stop_loss_condition": "跌破9", "review_trigger": "明日"}], "risk_controls": []},
        }),
        _json_response({"stage": "adversarial_review", "status": "ok", "summary": {"opposing_summary": "等待"}, "full": {"opposing_thesis": {}}}),
        _json_response({"stage": "judge_decision", "status": "ok", "summary": {"primary_plan_verdict": "accept_with_changes", "final_action": "wait", "decision_summary": "risk_off 等待", "next_step": "render_final_report"}, "full": {"winner": "mixed"}}),
    ]

    result = run_stock_selection_pipeline(
        task="我现在有5w元，帮我选股",
        agent_user_context=_context(),
        tool_registry=registry,
        llm_adapter=adapter,
        run_id="test-regime-risk-off",
    )

    allocation = result.final_report_json["portfolio_allocation"]
    assert allocation["summary"]["portfolio_action"] == "wait"
    assert allocation["summary"]["initial_total_position_pct"] == 0
    assert allocation["full"]["positions_plan"][0]["action"] == "wait"
    assert allocation["full"]["positions_plan"][0]["initial_position_pct"] == 0
    assert allocation["full"]["market_regime_constraint"]["regime"] == "risk_off"


def test_stock_selection_marks_partial_capital_flow_without_data_failed():
    registry = _registry()
    registry.register(ToolDefinition(
        name="get_capital_flow",
        description="flow",
        parameters=[ToolParameter(name="stock_code", type="string", description="code")],
        handler=lambda stock_code: {
            "stock_code": stock_code,
            "status": "partial",
            "main_net_inflow": None,
            "inflow_5d": None,
            "inflow_10d": None,
            "sector_rankings": {"top_inflow_sectors": [], "top_outflow_sectors": []},
            "errors": ["capital flow fetch failed"],
        },
    ))
    adapter = MagicMock()
    adapter.call_text.side_effect = [
        _json_response({"stage": "candidate_discovery", "status": "ok", "summary": {"candidate_codes": ["600001"]}, "full": {"candidates": [{"code": "600001", "name": "测试一"}]}}),
        _json_response({"stage": "candidate_screening", "status": "ok", "summary": {"deep_dive_targets": ["600001"]}, "full": {"shortlist": [{"code": "600001", "name": "测试一"}]}}),
        _json_response({"stage": "single_stock_deep_dive", "status": "ok", "summary": {"code": "600001", "name": "测试一", "action_bias": "wait"}, "full": {"stock": {"code": "600001", "name": "测试一"}}}),
        _json_response({"stage": "portfolio_allocation", "status": "ok", "summary": {"portfolio_action": "wait", "core_reason": "等待"}, "full": {"positions_plan": []}}),
        _json_response({"stage": "adversarial_review", "status": "ok", "summary": {"opposing_summary": "等待"}, "full": {"opposing_thesis": {}}}),
        _json_response({"stage": "judge_decision", "status": "ok", "summary": {"primary_plan_verdict": "accept", "final_action": "wait", "decision_summary": "等待", "next_step": "render_final_report"}, "full": {"winner": "mixed"}}),
    ]

    result = run_stock_selection_pipeline(
        task="我现在有5w元，帮我选股",
        agent_user_context=_context(),
        tool_registry=registry,
        llm_adapter=adapter,
        run_id="test-partial-flow",
    )

    assert result.success is True
    capital_flow_calls = [call for call in result.tool_calls_log if call["tool"] == "get_capital_flow"]
    assert capital_flow_calls
    assert all(call["success"] is False for call in capital_flow_calls)


def test_stock_selection_keeps_capital_flow_with_data_successful_when_errors_are_diagnostics():
    registry = _registry()
    registry.register(ToolDefinition(
        name="get_capital_flow",
        description="flow",
        parameters=[ToolParameter(name="stock_code", type="string", description="code")],
        handler=lambda stock_code: {
            "stock_code": stock_code,
            "status": "ok",
            "main_net_inflow": 123.4,
            "inflow_5d": None,
            "inflow_10d": None,
            "sector_rankings": {"top_inflow_sectors": [], "top_outflow_sectors": []},
            "errors": ["capital flow stage timeout"],
        },
    ))
    adapter = MagicMock()
    adapter.call_text.side_effect = [
        _json_response({"stage": "candidate_discovery", "status": "ok", "summary": {"candidate_codes": ["600001"]}, "full": {"candidates": [{"code": "600001", "name": "测试一"}]}}),
        _json_response({"stage": "candidate_screening", "status": "ok", "summary": {"deep_dive_targets": ["600001"]}, "full": {"shortlist": [{"code": "600001", "name": "测试一"}]}}),
        _json_response({"stage": "single_stock_deep_dive", "status": "ok", "summary": {"code": "600001", "name": "测试一", "action_bias": "wait"}, "full": {"stock": {"code": "600001", "name": "测试一"}}}),
        _json_response({"stage": "portfolio_allocation", "status": "ok", "summary": {"portfolio_action": "wait", "core_reason": "等待"}, "full": {"positions_plan": []}}),
        _json_response({"stage": "adversarial_review", "status": "ok", "summary": {"opposing_summary": "等待"}, "full": {"opposing_thesis": {}}}),
        _json_response({"stage": "judge_decision", "status": "ok", "summary": {"primary_plan_verdict": "accept", "final_action": "wait", "decision_summary": "等待", "next_step": "render_final_report"}, "full": {"winner": "mixed"}}),
    ]

    result = run_stock_selection_pipeline(
        task="我现在有5w元，帮我选股",
        agent_user_context=_context(),
        tool_registry=registry,
        llm_adapter=adapter,
        run_id="test-flow-errors",
    )

    assert result.success is True
    capital_flow_calls = [call for call in result.tool_calls_log if call["tool"] == "get_capital_flow"]
    assert capital_flow_calls
    assert all(call["success"] is True for call in capital_flow_calls)


def test_stock_selection_overwrites_mismatched_stock_name_by_code():
    registry = _registry()
    registry.register(ToolDefinition(
        name="discover_watchlist_candidates",
        description="discover",
        parameters=[],
        handler=lambda market="cn", seed_symbols=None, limit=8: {
            "status": "ok",
            "market": market,
            "candidates": [{"code": "301028", "name": "友升股份", "market": "cn", "source": "test"}],
        },
    ))
    registry.register(ToolDefinition(
        name="get_realtime_quote",
        description="quote",
        parameters=[ToolParameter(name="stock_code", type="string", description="code")],
        handler=lambda stock_code: {"code": stock_code, "name": "鼎熔岩", "price": 10.0},
    ))
    registry.register(ToolDefinition(
        name="get_stock_info",
        description="info",
        parameters=[ToolParameter(name="stock_code", type="string", description="code")],
        handler=lambda stock_code: {"code": stock_code, "name": "鼎熔岩", "belong_boards": []},
    ))
    adapter = MagicMock()
    adapter.call_text.side_effect = [
        _json_response({
            "stage": "candidate_discovery",
            "status": "ok",
            "summary": {"candidate_codes": ["301028"]},
            "full": {"candidates": [{"code": "301028", "name": "友升股份"}]},
        }),
        _json_response({
            "stage": "candidate_screening",
            "status": "ok",
            "summary": {"deep_dive_targets": ["301028"]},
            "full": {"shortlist": [{"code": "301028", "name": "友升股份", "screening_result": "deep_dive"}]},
        }),
        _json_response({
            "stage": "single_stock_deep_dive",
            "status": "ok",
            "summary": {"code": "301028", "name": "友升股份", "action_bias": "wait"},
            "full": {"stock": {"code": "301028", "name": "友升股份"}, "missing_evidence": []},
        }),
        _json_response({
            "stage": "portfolio_allocation",
            "status": "ok",
            "summary": {"portfolio_action": "wait", "core_reason": "等待"},
            "full": {
                "positions_plan": [
                    {
                        "rank": 1,
                        "code": "301028",
                        "name": "友升股份",
                        "action": "wait",
                        "initial_position_pct": 0,
                        "entry_condition": "等待确认",
                        "stop_loss_condition": "跌破支撑",
                        "review_trigger": "次日复查",
                    }
                ]
            },
        }),
        _json_response({"stage": "adversarial_review", "status": "ok", "summary": {"opposing_summary": "等待"}, "full": {"opposing_thesis": {}}}),
        _json_response({"stage": "judge_decision", "status": "ok", "summary": {"primary_plan_verdict": "accept", "final_action": "wait", "decision_summary": "等待", "next_step": "render_final_report"}, "full": {"winner": "mixed"}}),
    ]

    result = run_stock_selection_pipeline(
        task="分析 301028",
        agent_user_context=_context(),
        tool_registry=registry,
        llm_adapter=adapter,
        run_id="test-stock-identity",
    )

    report = result.final_report_json
    assert result.success is True
    assert report["candidate_discovery"]["full"]["candidates"][0]["name"] == "鼎熔岩"
    assert report["candidate_screening"]["full"]["shortlist"][0]["name"] == "鼎熔岩"
    assert report["single_stock_deep_dive"]["summary"]["results_brief"][0]["name"] == "鼎熔岩"
    assert report["portfolio_allocation"]["full"]["positions_plan"][0]["name"] == "鼎熔岩"
    assert "301028 鼎熔岩" in result.final_markdown
    assert "301028 友升股份" not in result.final_markdown
    audit = report["stock_identity_audit"]
    assert audit["status"] == "corrected"
    assert audit["violation_count"] >= 1
    assert any(item["code"] == "301028" and item["provided_name"] == "友升股份" for item in audit["violations"])


def test_stock_selection_portfolio_context_positions_do_not_require_name_field():
    context = _context()
    context.positions = [
        PositionContext(symbol="600519", quantity=100, avg_cost=1500, market_value=150000, position_pct=30)
    ]
    adapter = MagicMock()
    adapter.call_text.side_effect = [
        _json_response({"stage": "candidate_discovery", "status": "ok", "summary": {"candidate_codes": ["600001"]}, "full": {"candidates": [{"code": "600001", "name": "测试一"}]}}),
        _json_response({"stage": "candidate_screening", "status": "ok", "summary": {"deep_dive_targets": ["600001"]}, "full": {"shortlist": []}}),
        _json_response({"stage": "single_stock_deep_dive", "status": "ok", "summary": {"code": "600001", "name": "测试一", "action_bias": "wait"}, "full": {"stock": {"code": "600001", "name": "测试一"}, "missing_evidence": []}}),
        _json_response({"stage": "portfolio_allocation", "status": "ok", "summary": {"portfolio_action": "wait", "core_reason": "等待"}, "full": {"positions_plan": []}}),
        _json_response({"stage": "adversarial_review", "status": "ok", "summary": {"opposing_summary": "等待"}, "full": {"opposing_thesis": {}}}),
        _json_response({"stage": "judge_decision", "status": "ok", "summary": {"primary_plan_verdict": "accept", "final_action": "wait", "decision_summary": "等待", "next_step": "render_final_report"}, "full": {"winner": "mixed"}}),
    ]

    result = run_stock_selection_pipeline(
        task="我现在有5w元，帮我选股",
        agent_user_context=context,
        tool_registry=_registry_with_candidates([{"code": "600001", "name": "测试一", "market": "cn", "source": "test"}]),
        llm_adapter=adapter,
        run_id="test-position-context",
        orchestration_mode="expert_graph",
    )

    assert result.success is True
    assert result.error is None
    assert result.final_report_json["expert_state"]["orchestration_mode"] == "expert_graph"


def test_stock_selection_deep_dive_passes_stock_name_to_intel_tool():
    intel_calls = []
    registry = _registry_with_candidates([{"code": "600001", "name": "测试一", "market": "cn", "source": "test"}])
    registry.register(ToolDefinition(
        name="search_comprehensive_intel",
        description="intel",
        parameters=[
            ToolParameter(name="stock_code", type="string", description="code"),
            ToolParameter(name="stock_name", type="string", description="name"),
        ],
        handler=lambda stock_code, stock_name: intel_calls.append((stock_code, stock_name)) or {"report": "ok"},
    ))
    adapter = MagicMock()
    adapter.call_text.side_effect = [
        _json_response({"stage": "candidate_discovery", "status": "ok", "summary": {"candidate_codes": ["600001"]}, "full": {"candidates": [{"code": "600001", "name": "测试一"}]}}),
        _json_response({"stage": "candidate_screening", "status": "ok", "summary": {"deep_dive_targets": ["600001"]}, "full": {"shortlist": []}}),
        _json_response({"stage": "single_stock_deep_dive", "status": "ok", "summary": {"code": "600001", "name": "测试一", "action_bias": "wait"}, "full": {"stock": {"code": "600001", "name": "测试一"}, "missing_evidence": []}}),
        _json_response({"stage": "portfolio_allocation", "status": "ok", "summary": {"portfolio_action": "wait", "core_reason": "等待"}, "full": {"positions_plan": []}}),
        _json_response({"stage": "adversarial_review", "status": "ok", "summary": {"opposing_summary": "等待"}, "full": {"opposing_thesis": {}}}),
        _json_response({"stage": "judge_decision", "status": "ok", "summary": {"primary_plan_verdict": "accept", "final_action": "wait", "decision_summary": "等待", "next_step": "render_final_report"}, "full": {"winner": "mixed"}}),
    ]

    result = run_stock_selection_pipeline(
        task="我现在有5w元，帮我选股",
        agent_user_context=_context(),
        tool_registry=registry,
        llm_adapter=adapter,
        run_id="test-intel-args",
    )

    assert result.success is True
    assert intel_calls == [("600001", "600001名称")]


def test_stock_selection_stage_failure_still_returns_expert_graph_partial_report():
    adapter = MagicMock()
    adapter.call_text.side_effect = [
        _json_response({"stage": "candidate_discovery", "status": "ok", "summary": {"candidate_codes": ["600001"]}, "full": {"candidates": [{"code": "600001", "name": "测试一"}]}}),
        RuntimeError("llm provider timeout"),
    ]

    result = run_stock_selection_pipeline(
        task="我现在有5w元，帮我选股",
        agent_user_context=_context(),
        tool_registry=_registry(),
        llm_adapter=adapter,
        run_id="test-partial-expert-graph",
        orchestration_mode="expert_graph",
    )

    assert result.success is True
    assert result.error == "llm provider timeout"
    assert result.final_report_json["partial_failure"]["status"] == "failed_stage_degraded"
    assert result.final_report_json["orchestration_mode"] == "expert_graph"
    assert result.final_report_json["expert_state"]["orchestration_mode"] == "expert_graph"
    assert result.final_report_json["judge_decision"]["summary"]["final_action"] == "wait"


def test_selection_run_context_default_candidate_discovery_mode_is_deterministic():
    ctx = SelectionRunContext(run_id="r", user_message="m")
    assert ctx.candidate_discovery_mode == "deterministic"


def test_resolve_candidate_discovery_mode_falls_back_for_invalid_value():
    assert _resolve_candidate_discovery_mode(None) == "thesis_desk_committee"
    assert _resolve_candidate_discovery_mode("") == "thesis_desk_committee"
    assert _resolve_candidate_discovery_mode("deterministic") == "deterministic"
    assert _resolve_candidate_discovery_mode("llm_expert_committee") == "llm_expert_committee"
    assert _resolve_candidate_discovery_mode("nonsense_value") == "thesis_desk_committee"


def test_run_candidate_discovery_tool_exits_when_mode_is_not_thesis_desk():
    events: list = []
    ctx = SelectionRunContext(
        run_id="r",
        user_message="m",
        candidate_discovery_mode="llm_expert_committee",
        progress_callback=lambda evt: events.append(evt),
    )
    with patch(
        "src.agent.candidate_experts_v2.committee.run_committee_discovery",
        side_effect=AssertionError("committee should not run"),
    ) as mocked:
        result = _run_candidate_discovery_tool(
            ctx=ctx,
            tool_registry=_registry(),
            target_symbols=["000001"],
        )
    assert result["status"] == "skipped"
    assert result["candidate_count"] == 0
    assert result["full"]["required_candidate_discovery_mode"] == "thesis_desk_committee"
    assert result["full"]["actual_candidate_discovery_mode"] == "llm_expert_committee"
    mocked.assert_not_called()
    event_types = [e.get("type") or e.get("event") for e in events if isinstance(e, dict)]
    assert event_types == ["selection_candidate_discovery_mode"]


def test_run_candidate_discovery_tool_uses_committee_when_mode_is_thesis_desk():
    events: list = []
    ctx = SelectionRunContext(
        run_id="r",
        user_message="m",
        candidate_discovery_mode="thesis_desk_committee",
        progress_callback=lambda evt: events.append(evt),
    )
    committee_payload = {
        "status": "ok",
        "candidates": [{"code": "000001", "name": "测试"}],
        "seed_pool_summary": {
            "seed_count": 3,
            "seed_sources": {"user_watchlist": 1, "low_base_structure": 2},
            "total_limit": 40,
            "preview": [{"code": "000001", "source": "user_watchlist"}],
        },
    }
    with patch(
        "src.agent.candidate_experts_v2.committee.run_committee_discovery",
        return_value=committee_payload,
    ) as mocked:
        result = _run_candidate_discovery_tool(
            ctx=ctx,
            tool_registry=_registry(),
            target_symbols=["000001"],
        )
    assert result is committee_payload
    assert mocked.called
    assert result["seed_pool_summary"]["seed_count"] == 3
    event_types = [e.get("type") or e.get("event") for e in events if isinstance(e, dict)]
    assert event_types[:3] == [
        "selection_candidate_discovery_mode",
        "selection_seed_pool_built",
        "selection_seed_pool_quality_snapshot",
    ]
    assert "selection_seed_gate_done" in event_types
    assert event_types.index("selection_seed_pool_quality_snapshot") < event_types.index(
        "selection_seed_gate_done",
    )
    seed_event = next(e for e in events if e.get("type") == "selection_seed_pool_built")
    assert seed_event["payload"]["seed_pool_summary"]["seed_count"] >= 1
    assert seed_event["payload"]["seed_pool_diagnostics"]
    assert "seed_market_regime" in seed_event["payload"]
    gate_event = next(e for e in events if e.get("type") == "selection_seed_gate_done")
    assert gate_event["payload"]["seed_pool_summary"]["seed_count"] == 3


def test_seed_pool_total_limit_preserves_user_watchlist_without_online_sources():
    from src.agent.candidate_experts_v2.committee import _build_seed_pool_result

    class _NoOnlineRegistry:
        def execute(self, *args, **kwargs):
            raise AssertionError("online sources should not run when user seeds fill total_limit")

    result = _build_seed_pool_result(
        market="cn",
        seed_symbols=["600519"],
        tool_registry=_NoOnlineRegistry(),
        today="20260529",
        total_limit=1,
    )

    assert [seed.code for seed in result.seeds] == ["600519"]
    assert result.seeds[0].source == "user_watchlist"
    assert result.diagnostics[0]["source"] == "user_watchlist"
    assert result.diagnostics[0]["count"] == 1


def test_fallback_candidate_discovery_preserves_seed_pool_metadata():
    from src.agent.stock_selection import _fallback_candidate_discovery

    ctx = SelectionRunContext(run_id="r", user_message="m", candidate_discovery_mode="llm_expert_committee")
    seed_result = {
        "candidates": [{"code": "000001", "name": "测试", "source": "user_watchlist"}],
        "seed_pool_summary": {"seed_count": 1, "seed_sources": {"user_watchlist": 1}},
        "seed_pool_summary_before_gate": {"seed_count": 2},
        "seed_gate": {"status": "ok", "kept_count": 1, "rejected_count": 1},
        "seed_pool_diagnostics": [{"source": "user_watchlist", "status": "ok", "count": 1}],
        "seed_pool_hard_exclusion": {"excluded_count": 0},
        "seed_source_quality": {"user_watchlist": {"status": "ok"}},
        "seed_market_regime": {"regime": "range_bound"},
    }

    payload = _fallback_candidate_discovery(ctx, seed_result)

    assert payload["seed_pool_summary"]["seed_count"] == 1
    assert payload["full"]["seed_gate"]["status"] == "ok"
    assert payload["full"]["seed_pool_diagnostics"][0]["source"] == "user_watchlist"
    assert payload["full"]["seed_market_regime"]["regime"] == "range_bound"


def test_seed_pool_quality_snapshot_before_open_uses_previous_seed_date():
    ctx = SelectionRunContext(run_id="selection-run", user_message="m", candidate_discovery_mode="llm_expert_committee")
    payload = {
        "status": "ok",
        "seed_pool_summary": {
            "seed_count": 1,
            "preview": [{"code": "600001", "name": "测试一", "source": "daily_screener"}],
        },
    }

    service_instance = MagicMock()
    service_instance.persist_candidate_discovery_snapshot.return_value = {"status": "ok", "snapshot_id": 1}
    fixed_now = datetime(2026, 6, 10, 8, 59)
    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return fixed_now if tz is None else fixed_now.replace(tzinfo=tz)

    with patch("src.services.seed_pool_quality_service.SeedPoolQualityService", return_value=service_instance), \
            patch("src.agent.stock_selection.datetime", FixedDateTime):
        _persist_seed_pool_quality_snapshot(ctx=ctx, payload=payload, today="20260610")

    kwargs = service_instance.persist_candidate_discovery_snapshot.call_args.kwargs
    assert kwargs["seed_date"].isoformat() == "2026-06-09"
    assert kwargs["run_id"] == "selection-run:2026-06-09"
    assert kwargs["trace_id"] == "selection-run:2026-06-09"
    assert kwargs["generated_at"] == fixed_now


def test_seed_pool_quality_snapshot_uses_seed_freshness_and_trace_id():
    ctx = SelectionRunContext(run_id="trace-abc123", user_message="m", candidate_discovery_mode="thesis_desk_committee")
    payload = {
        "status": "ok",
        "seed_pool_summary": {
            "seed_count": 1,
            "preview": [
                {
                    "code": "002718",
                    "name": "友邦吊顶",
                    "source": "daily_screener",
                    "freshness": "20260611",
                }
            ],
        },
    }

    service_instance = MagicMock()
    service_instance.persist_candidate_discovery_snapshot.return_value = {"status": "ok", "snapshot_id": 1}
    fixed_now = datetime(2026, 6, 12, 14, 20)

    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return fixed_now if tz is None else fixed_now.replace(tzinfo=tz)

    with patch("src.services.seed_pool_quality_service.SeedPoolQualityService", return_value=service_instance), \
            patch("src.agent.stock_selection.datetime", FixedDateTime):
        _persist_seed_pool_quality_snapshot(ctx=ctx, payload=payload, today="20260612")

    kwargs = service_instance.persist_candidate_discovery_snapshot.call_args.kwargs
    assert kwargs["seed_date"].isoformat() == "2026-06-11"
    assert kwargs["run_id"] == "trace-abc123:2026-06-11"
    assert kwargs["trace_id"] == "trace-abc123"
    assert kwargs["generated_at"] == fixed_now


def test_run_candidate_discovery_tool_returns_failed_payload_when_thesis_committee_raises():
    events: list = []
    ctx = SelectionRunContext(
        run_id="r",
        user_message="m",
        candidate_discovery_mode="thesis_desk_committee",
        progress_callback=lambda evt: events.append(evt),
    )
    with patch(
        "src.agent.candidate_experts_v2.committee.run_committee_discovery",
        side_effect=RuntimeError("llm timeout"),
    ):
        result = _run_candidate_discovery_tool(
            ctx=ctx,
            tool_registry=_registry(),
            target_symbols=[],
        )
    assert isinstance(result, dict)
    assert result.get("status") == "failed"
    assert result.get("candidates") == []
    flat = []
    for evt in events:
        payload = evt.get("payload") if isinstance(evt, dict) else None
        if isinstance(payload, dict):
            flat.append(payload)
        if isinstance(evt, dict):
            flat.append(evt)
    assert any(item.get("fallback") is False for item in flat), f"expected non-fallback event, got {events!r}"


def test_stock_selection_fails_fast_when_thesis_desk_discovery_fails():
    adapter = MagicMock()
    adapter.call_text.side_effect = AssertionError("downstream stages should not run")
    failed_discovery = {
        "stage": "candidate_discovery",
        "status": "failed",
        "market": "cn",
        "candidates": [],
        "candidate_count": 0,
        "candidate_source": "thesis_desk_committee",
        "error": "thesis_desk_committee returned no candidates",
        "seed_pool_summary": {"seed_count": 20},
        "thesis_desk_packets": [
            {
                "expert": "early_turn_desk",
                "status": "timeout",
                "errors": ["early_turn_desk seed 600001 timeout after 3.0s"],
            }
        ],
        "discovery_steps": [
            {
                "source": "thesis_desk_committee",
                "status": "failed",
                "error": "thesis_desk_committee returned no candidates",
                "fallback": False,
            }
        ],
    }

    with patch(
        "src.agent.stock_selection._run_candidate_discovery_tool",
        return_value=failed_discovery,
    ):
        result = run_stock_selection_pipeline(
            task="帮我选股",
            agent_user_context=_context(),
            tool_registry=_registry(),
            llm_adapter=adapter,
            run_id="test-thesis-fail-fast",
        )

    assert result.success is False
    assert result.error
    assert "三席位候选发现失败" in result.error
    assert "early_turn_desk seed 600001 timeout" in result.error
    assert result.context.next_step == "stop_candidate_discovery_failed"
    assert "candidate_screening" not in result.context.stages
    assert "single_stock_deep_dive" not in result.context.stages
    assert result.final_report_json["partial_failure"]["status"] == "failed_candidate_discovery"
    assert result.final_report_json["partial_failure"]["fallback"] is False


def test_stock_selection_pipeline_exits_when_candidate_mode_is_not_thesis_desk():
    registry = _registry()
    adapter = MagicMock()

    result = run_stock_selection_pipeline(
        task="帮我选一下可以入手的股票",
        agent_user_context=_context(),
        tool_registry=registry,
        llm_adapter=adapter,
        run_id="test-non-thesis-exit",
        candidate_discovery_mode="deterministic",
    )

    assert result.success is True
    assert result.context.next_step == "stop_non_thesis_desk_mode"
    discovery = result.final_report_json["candidate_discovery"]
    assert discovery["status"] == "skipped"
    assert discovery["summary"]["candidate_codes"] == []
    assert discovery["full"]["blocked"] is True
    assert discovery["full"]["required_candidate_discovery_mode"] == "thesis_desk_committee"
    assert result.tool_calls_log == []
    adapter.call_text.assert_not_called()
