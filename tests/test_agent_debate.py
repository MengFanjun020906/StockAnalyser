import json
from unittest.mock import MagicMock

from src.agent.debate import format_debate_appendix, run_adversarial_debate, should_run_debate
from src.agent.llm_adapter import LLMResponse
from src.schemas.agent_context import AgentUserContext, PositionContext, ReportContext


def _tool_calls():
    return [
        {
            "step": 1,
            "tool": "get_realtime_quote",
            "arguments": {"stock_code": "600519"},
            "success": True,
            "duration": 0.1,
            "result_preview": '{"price": 100, "quote_trade_date": "20260501"}',
        }
    ]


def test_should_run_debate_for_position_review_with_shared_evidence():
    context = AgentUserContext(
        positions=[PositionContext(symbol="600519", quantity=100, avg_cost=90)],
        report=ReportContext(
            analysis_mode="planning_execute",
            intent="position_review",
            primary_symbol="600519",
            target_symbols=["600519"],
        ),
    )

    assert should_run_debate(agent_user_context=context, tool_calls=_tool_calls()) is True


def test_debate_runs_primary_opposing_and_judge_for_position_review():
    context = AgentUserContext(
        positions=[PositionContext(symbol="600519", quantity=100, avg_cost=90)],
        report=ReportContext(
            analysis_mode="planning_execute",
            intent="position_review",
            primary_symbol="600519",
            target_symbols=["600519"],
        ),
    )
    adapter = MagicMock()
    adapter.call_text.side_effect = [
        LLMResponse(
            content=json.dumps({
                "direction": "bullish",
                "action": "hold",
                "summary": "价格仍在成本上方，主观点支持继续持有。",
                "evidence": ["price=100", "avg_cost=90"],
                "evidence_by_dimension": {
                    "technical": ["price=100"],
                    "capital_flow": ["资金面数据缺失"],
                    "news_event": ["消息面数据缺失"],
                },
                "failure_conditions": ["跌破 90"],
                "account_impact": "仓位需要控制。",
            }, ensure_ascii=False),
            provider="openai",
            model="openai/test",
            usage={"total_tokens": 10},
        ),
        LLMResponse(
            content=json.dumps({
                "direction": "neutral_bearish",
                "action": "reduce",
                "summary": "反方认为若仓位偏高，应先降低暴露。",
                "evidence": ["仓位约束"],
                "evidence_by_dimension": {
                    "account_risk": ["仓位约束"],
                    "capital_flow": ["未获取主力资金流"],
                    "news_event": ["未确认消息催化"],
                },
                "failure_conditions": ["放量突破且风险可控"],
                "primary_challenges": ["主观点没有证明仓位安全"],
                "account_impact": "回撤会影响账户。",
            }, ensure_ascii=False),
            provider="openai",
            model="openai/test",
            usage={"total_tokens": 11},
        ),
        LLMResponse(
            content=json.dumps({
                "winner": "primary",
                "final_action": "hold",
                "decision_summary": "持有证据更强，但资金面和消息面证据不足。",
                "reason": "证据支持持有，但采纳反方仓位风险。",
                "reason_points": ["成本上方支持持有", "仓位风险需要控制", "资金面和消息面证据不足"],
                "dimension_assessments": [
                    {
                        "dimension": "account_risk",
                        "verdict": "mixed",
                        "weight": "high",
                        "summary": "仓位风险需要控制。",
                        "evidence": ["avg_cost=90"],
                        "missing": [],
                    },
                    {
                        "dimension": "capital_flow",
                        "verdict": "insufficient_data",
                        "weight": "medium",
                        "summary": "资金面未形成强证据。",
                        "evidence": [],
                        "missing": ["未调用 get_capital_flow"],
                    },
                ],
                "accepted_arguments": ["成本上方", "仓位风险需要控制"],
                "rejected_arguments": ["立即减仓证据不足"],
                "risk_controls": ["跌破成本区复查"],
                "unresolved_conflicts": [],
            }, ensure_ascii=False),
            provider="openai",
            model="openai/test",
            usage={"total_tokens": 12},
        ),
    ]

    events = []
    result = run_adversarial_debate(
        task="我持有 600519，适合继续拿长线吗？",
        primary_report="主报告：持有。",
        agent_user_context=context,
        planner={"intent": "position_review"},
        tool_calls=_tool_calls(),
        llm_adapter=adapter,
        progress_callback=events.append,
    )

    assert result.success is True
    assert result.intent == "position_review"
    assert result.primary_thesis["action"] == "hold"
    assert result.primary_thesis["evidence_by_dimension"]["capital_flow"] == ["资金面数据缺失"]
    assert result.opposing_thesis["action"] == "reduce"
    assert result.judge_decision["final_action"] == "hold"
    assert result.judge_decision["decision_summary"] == "持有证据更强，但资金面和消息面证据不足。"
    assert result.judge_decision["dimension_assessments"][1]["dimension"] == "capital_flow"
    assert result.total_tokens == 33
    assert result.debug_outputs["primary_report_raw"] == "主报告：持有。"
    assert "价格仍在成本上方" in result.debug_outputs["primary_thesis_raw"]
    assert "反方认为若仓位偏高" in result.debug_outputs["opposing_thesis_raw"]
    assert "证据支持持有" in result.debug_outputs["judge_raw"]
    assert [event["type"] for event in events] == [
        "debate_start",
        "debate_primary_done",
        "debate_opposing_done",
        "debate_judge_done",
    ]
    assert adapter.call_text.call_count == 3
    opposing_prompt = adapter.call_text.call_args_list[1].args[0][1]["content"]
    assert "不能另行调用工具" in opposing_prompt
    assert "同一份证据包" in opposing_prompt
    assert "get_realtime_quote" in opposing_prompt

    appendix = format_debate_appendix(result)
    assert "## 对抗式辩论裁决" in appendix
    assert "Judge 最终裁决" in appendix
    assert "分维度证据" in appendix
    assert "资金面未形成强证据" in appendix


def test_debate_entry_analysis_forces_wait_or_reject_opposition():
    context = AgentUserContext(
        report=ReportContext(
            analysis_mode="planning_execute",
            intent="entry_analysis",
            primary_symbol="600519",
            target_symbols=["600519"],
        ),
    )
    adapter = MagicMock()
    adapter.call_text.side_effect = [
        LLMResponse(content='{"direction":"bullish","action":"open","summary":"支持小仓试错","evidence":["price=100"],"failure_conditions":["跌破止损"],"account_impact":"首仓受限"}'),
        LLMResponse(content='{"direction":"neutral_bearish","action":"wait","summary":"反方主张等待确认","evidence":["追高风险"],"failure_conditions":["回踩确认"],"primary_challenges":["风险收益比不足"],"account_impact":"避免无效开仓"}'),
        LLMResponse(content='{"winner":"opposing","final_action":"wait","decision_summary":"入场证据不足，等待确认。","reason":"入场证据不足","reason_points":["资金面证据不足","消息面证据不足"],"dimension_assessments":[{"dimension":"capital_flow","verdict":"insufficient_data","weight":"medium","summary":"资金面未确认承接","evidence":[],"missing":["未调用 get_capital_flow"]}],"accepted_arguments":["等待确认"],"rejected_arguments":["立即开仓"],"risk_controls":["确认止损后再评估"],"unresolved_conflicts":[]}'),
    ]

    result = run_adversarial_debate(
        task="600519 适合买入吗？",
        primary_report="主报告：可以观察。",
        agent_user_context=context,
        planner={"intent": "entry_analysis"},
        tool_calls=_tool_calls(),
        llm_adapter=adapter,
    )

    assert result.success is True
    assert result.intent == "entry_analysis"
    assert result.opposing_thesis["action"] == "wait"
    assert result.judge_decision["winner"] == "opposing"
    opposing_prompt = adapter.call_text.call_args_list[1].args[0][1]["content"]
    assert "反对现在入场" in opposing_prompt
    judge_prompt = adapter.call_text.call_args_list[2].args[0][1]["content"]
    assert "资金面和消息面不能被技术面覆盖" in judge_prompt


def test_debate_watchlist_scan_runs_judge_for_stock_selection():
    context = AgentUserContext(
        report=ReportContext(
            analysis_mode="planning_execute",
            intent="watchlist_scan",
            target_symbols=[],
            include_watchlist_ranking=True,
        ),
    )
    adapter = MagicMock()
    adapter.call_text.side_effect = [
        LLMResponse(content='{"direction":"neutral_bullish","action":"monitor","summary":"支持当前候选排序","evidence":["板块强势"],"failure_conditions":["候选回撤破位"],"account_impact":"5万元应分批配置"}'),
        LLMResponse(content='{"direction":"neutral_bearish","action":"wait","summary":"反方主张等待回踩","evidence":["候选有追高风险"],"failure_conditions":["缩量企稳"],"primary_challenges":["仓位配置偏激进"],"account_impact":"控制首仓"}'),
        LLMResponse(content='{"winner":"opposing","final_action":"wait","decision_summary":"候选可跟踪，但当前不宜立即配置。","reason":"选股证据不足以支持立即开仓","reason_points":["候选存在追高风险"],"dimension_assessments":[{"dimension":"technical","verdict":"mixed","weight":"high","summary":"趋势强但买点不舒服","evidence":["板块强势"],"missing":[]}],"accepted_arguments":["等待回踩"],"rejected_arguments":["立即满配"],"risk_controls":["首仓不超过20%"],"unresolved_conflicts":[]}'),
    ]

    result = run_adversarial_debate(
        task="我现在有5w元，帮我选股并分配仓位",
        primary_report="主报告：候选中芯国际和赣锋锂业。",
        agent_user_context=context,
        planner={"intent": "watchlist_scan"},
        tool_calls=_tool_calls(),
        llm_adapter=adapter,
    )

    assert should_run_debate(agent_user_context=context, tool_calls=_tool_calls()) is True
    assert result.success is True
    assert result.intent == "watchlist_scan"
    assert result.judge_decision["final_action"] == "wait"
    primary_prompt = adapter.call_text.call_args_list[0].args[0][1]["content"]
    assert "候选排序" in primary_prompt


def test_debate_judge_parses_final_json_after_explanatory_object():
    context = AgentUserContext(
        report=ReportContext(
            analysis_mode="planning_execute",
            intent="entry_analysis",
            primary_symbol="600519",
            target_symbols=["600519"],
        ),
    )
    adapter = MagicMock()
    adapter.call_text.side_effect = [
        LLMResponse(content='{"direction":"bullish","action":"open","summary":"支持观察入场","evidence":["price=100"],"failure_conditions":["跌破止损"],"account_impact":"小仓"}'),
        LLMResponse(content='{"direction":"neutral_bearish","action":"wait","summary":"反方主张等待","evidence":["证据不足"],"failure_conditions":["补足证据"],"primary_challenges":["资金面缺失"],"account_impact":"降低试错成本"}'),
        LLMResponse(content=(
            '我先给一个草稿对象：{"winner":"insufficient_data","final_action":"monitor"}\n'
            '最终 JSON 如下：\n'
            '```json\n'
            '{"winner":"opposing","final_action":"wait","decision_summary":"等待资金面和消息面确认。","reason":"证据不足","reason_points":["资金面缺失"],"dimension_assessments":[{"dimension":"data_quality","verdict":"insufficient_data","weight":"high","summary":"结构化证据不足","evidence":[],"missing":["资金面"]}],"accepted_arguments":["等待"],"rejected_arguments":["立即开仓"],"risk_controls":["补齐资金面后复查"],"unresolved_conflicts":[]}\n'
            '```'
        )),
    ]

    result = run_adversarial_debate(
        task="600519 适合买入吗？",
        primary_report="主报告：可以观察。",
        agent_user_context=context,
        planner={"intent": "entry_analysis"},
        tool_calls=_tool_calls(),
        llm_adapter=adapter,
    )

    assert result.success is True
    assert result.judge_decision["winner"] == "opposing"
    assert result.judge_decision["final_action"] == "wait"
    assert "judge_parse_error" not in result.debug_outputs


def test_debate_records_parse_error_when_judge_json_unrecoverable():
    context = AgentUserContext(
        report=ReportContext(
            analysis_mode="planning_execute",
            intent="entry_analysis",
            primary_symbol="600519",
            target_symbols=["600519"],
        ),
    )
    adapter = MagicMock()
    adapter.call_text.side_effect = [
        LLMResponse(content='{"direction":"bullish","action":"open","summary":"支持观察入场","evidence":["price=100"],"failure_conditions":["跌破止损"],"account_impact":"小仓"}'),
        LLMResponse(content='{"direction":"neutral_bearish","action":"wait","summary":"反方主张等待","evidence":["证据不足"],"failure_conditions":["补足证据"],"primary_challenges":["资金面缺失"],"account_impact":"降低试错成本"}'),
        LLMResponse(content="这不是 JSON，也没有完整对象。"),
    ]

    result = run_adversarial_debate(
        task="600519 适合买入吗？",
        primary_report="主报告：可以观察。",
        agent_user_context=context,
        planner={"intent": "entry_analysis"},
        tool_calls=_tool_calls(),
        llm_adapter=adapter,
    )

    assert result.success is True
    assert result.judge_decision["winner"] == "insufficient_data"
    assert result.debug_outputs["judge_parse_error"] == "llm_json_parse_failed:judge_decision"
