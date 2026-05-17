import json
import sys
from unittest.mock import MagicMock

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
from src.agent.stock_selection import render_stock_selection_markdown, run_stock_selection_pipeline, should_run_stock_selection
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
                {"code": "600002", "name": "测试二", "market": "cn", "source": "test"},
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


def _json_response(payload, tokens=1):
    return LLMResponse(
        content=json.dumps(payload, ensure_ascii=False),
        usage={"total_tokens": tokens},
        provider="openai",
        model="openai/test",
    )


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
    recommendation_section = markdown.split("## 三、Execute 证据摘要", 1)[0]

    assert "| 首选标的 | 暂无可入手标的 |" in markdown
    assert "本轮没有形成可直接入手的股票" in recommendation_section
    assert "### 🥇 首选：000568 泸州老窖" not in markdown
    assert "### 观察池" in recommendation_section
    assert "| 000568 泸州老窖 | 等待确认 / weak |" in recommendation_section
    assert "| 300283 温州宏丰 | 候选观察 |" in recommendation_section


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

    conclusion_block = markdown.split("## 六、辅助审查摘要", 1)[0]
    assert "组合主结论：候选质量尚可但等待回踩确认。" in conclusion_block
    assert "如果它进入核心原因" not in conclusion_block
    assert "## 六、辅助审查摘要" in markdown
    assert "反方审查与 Judge 裁决" not in markdown
    assert "风险三" in markdown
    assert "风险四" not in markdown
    assert "缺口一" in markdown
    assert "缺口三" not in markdown
    assert "缺口四" not in markdown


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
    candidate_section = markdown.split("## 附录一、候选池来源与入池理由", 1)[1].split("## 附录二", 1)[0]

    assert "| 排名 | 股票 | 入池通道 | 入池分 | 深度分析 | 入池理由 | 关注点 |" in candidate_section
    assert candidate_section.index("| 1 | 301183 东田微") < candidate_section.index("| 2 | 000568 泸州老窖")
    assert candidate_section.index("| 2 | 000568 泸州老窖") < candidate_section.index("| 3 | 300572 安车检测")
    assert "| 2 | 000568 泸州老窖 | 基本面质量筛选 | 100 | 已完成 |" in candidate_section
    assert "未覆盖（深挖上限 6 只）" in candidate_section
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
        tool_registry=_registry(),
        llm_adapter=adapter,
        run_id="test-run",
        orchestration_mode="legacy",
    )

    assert result.success is True
    assert "选股分析报告：下周可关注候选" in result.final_markdown
    assert "核心推荐结论" in result.final_markdown
    assert "推荐排序与入场决策" in result.final_markdown
    assert "Execute 证据摘要" in result.final_markdown
    assert "候选池来源与入池理由" in result.final_markdown
    assert "逐股维度证据展开" in result.final_markdown
    assert "600001 测试一" in result.final_markdown
    assert "MA5/MA20 多头" in result.final_markdown
    assert "跌破 9 元支撑" in result.final_markdown
    assert "capital_flow" in result.final_markdown
    assert result.context.stage_summary("judge_decision")["final_action"] == "wait"
    assert result.context.stages["candidate_discovery"].full_ref == "candidate_discovery.json"
    assert adapter.call_text.call_count == 6
    assert result.total_tokens == 6
    assert any(call["tool"] == "discover_watchlist_candidates" for call in result.tool_calls_log)
    assert any(call["tool"] == "detect_market_regime" for call in result.tool_calls_log)
    assert result.final_report_json["market_regime"]["regime"] == "trending_up"
    assert result.final_report_json["orchestration_mode"] == "legacy"
    assert result.final_report_json["expert_state"] is None
    capital_flow_calls = [call for call in result.tool_calls_log if call["tool"] == "get_capital_flow"]
    assert capital_flow_calls
    assert all(call["success"] is False for call in capital_flow_calls)
    assert "get_capital_flow" in result.context.evidence_ledger["summary"]["failed_tools"]


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
        tool_registry=_registry(),
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
        tool_registry=_registry(),
        llm_adapter=adapter,
        run_id="test-reject-downgrade",
        orchestration_mode="legacy",
    )

    judge = result.final_report_json["judge_decision"]["summary"]
    assert judge["final_action"] == "wait"
    assert judge["primary_plan_verdict"] == "wait_for_more_data"
    assert judge["next_step"] == "render_final_report"
    assert "候选池已形成" in judge["decision_summary"]
    assert "候选池来源与入池理由" in result.final_markdown
    assert "AlphaSift 多因子策略入池" in result.final_markdown


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
    assert adapter.call_text.call_count == 8
    assert "600001 测试一" in result.final_markdown
    assert "600002 测试二" in result.final_markdown
    assert "600003 测试三" in result.final_markdown
    assert "600004 测试四" not in result.final_markdown


def test_stock_selection_applies_judge_monitor_override_to_portfolio_plan():
    adapter = MagicMock()
    adapter.call_text.side_effect = [
        _json_response({"stage": "candidate_discovery", "status": "ok", "summary": {"candidate_codes": ["300572"]}, "full": {"candidates": [{"code": "300572", "name": "安车检测"}]}}),
        _json_response({"stage": "candidate_screening", "status": "ok", "summary": {"deep_dive_targets": ["300572"]}, "full": {"shortlist": [{"code": "300572", "name": "安车检测", "screening_result": "deep_dive"}]}}),
        _json_response({"stage": "single_stock_deep_dive", "status": "ok", "summary": {"code": "300572", "name": "安车检测", "action_bias": "wait", "action_strength": "weak"}, "full": {"stock": {"code": "300572", "name": "安车检测"}, "missing_evidence": ["fundamental"]}}),
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
        tool_registry=_registry(),
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
        tool_registry=_registry(),
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
    registry = _registry()
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


def test_stock_selection_marks_capital_flow_with_errors_failed_even_with_data():
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
    assert all(call["success"] is False for call in capital_flow_calls)


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
        tool_registry=_registry(),
        llm_adapter=adapter,
        run_id="test-position-context",
        orchestration_mode="expert_graph",
    )

    assert result.success is True
    assert result.error is None
    assert result.final_report_json["expert_state"]["orchestration_mode"] == "expert_graph"


def test_stock_selection_deep_dive_passes_stock_name_to_intel_tool():
    intel_calls = []
    registry = _registry()
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
