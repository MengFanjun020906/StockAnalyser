from src.agent.planning_prompts import (
    CANDIDATE_POOL_PROTOCOL,
    CONSTRAINTS,
    DEBATE_PROTOCOL,
    ENTRY_ANALYSIS_OUTPUT_FORMAT,
    EVENT_TRIGGER_POLICY,
    EXECUTE_PROTOCOL,
    EN_SYSTEM_PROMPT,
    POSITION_REVIEW_OUTPUT_FORMAT,
    TOOL_USE_POLICY,
    WATCHLIST_SCAN_OUTPUT_FORMAT,
    ZH_SYSTEM_PROMPT,
    PromptBuildOptions,
    build_planning_system_prompt,
    build_zh_planning_system_prompt,
    get_default_prompt_sections,
)


def test_default_zh_prompt_uses_single_section_source():
    prompt = build_planning_system_prompt()

    assert ZH_SYSTEM_PROMPT == prompt
    assert build_zh_planning_system_prompt() == prompt
    assert len(get_default_prompt_sections()) == 17


def test_default_zh_prompt_contains_phase_one_contract_sections():
    prompt = build_planning_system_prompt()

    required_snippets = [
        "你是 StockAnalyser Agent",
        "## 分析维度与能力域",
        "## Planning -> Execute 协议",
        "## Watchlist Candidate Pool Protocol",
        "## Execute Protocol",
        "## Debate Protocol",
        "### Planner 角色边界",
        "### todo.md 风格计划格式",
        "### 工具计划规范",
        "### 结构化执行计划",
        "## 约束规则",
        "## 账户感知规则",
        "## 重大事件触发规则",
        "## 持仓报告输出规范（position_review）",
        "## 入场报告输出规范（entry_analysis）",
        "## 选股报告输出规范（watchlist_scan）",
        "持仓动作表格",
        "执行动作矩阵",
        "入场决策表格",
        "分层建仓计划",
    ]
    for snippet in required_snippets:
        assert snippet in prompt


def test_prompt_options_can_remove_optional_policy_sections():
    prompt = build_planning_system_prompt(
        PromptBuildOptions(include_tool_policy=False, include_event_policy=False)
    )

    assert TOOL_USE_POLICY not in prompt
    assert EVENT_TRIGGER_POLICY not in prompt
    assert EXECUTE_PROTOCOL in prompt
    assert CANDIDATE_POOL_PROTOCOL in prompt
    assert DEBATE_PROTOCOL in prompt
    assert CONSTRAINTS in prompt
    assert POSITION_REVIEW_OUTPUT_FORMAT in prompt
    assert ENTRY_ANALYSIS_OUTPUT_FORMAT in prompt


def test_prompt_extra_instructions_are_appended_without_dropping_contract():
    prompt = build_zh_planning_system_prompt(extra_instructions="只分析用户指定股票。")

    assert "## 额外指令" in prompt
    assert "只分析用户指定股票。" in prompt
    assert "## 约束规则" in prompt
    assert "## 持仓报告输出规范（position_review）" in prompt
    assert "## 入场报告输出规范（entry_analysis）" in prompt


def test_position_review_output_does_not_expose_confidence_field():
    forbidden_public_fields = [
        "| 置信度 |",
        '"confidence"',
        "confidence_level",
    ]

    for forbidden in forbidden_public_fields:
        assert forbidden not in POSITION_REVIEW_OUTPUT_FORMAT
        assert forbidden not in build_planning_system_prompt()


def test_position_review_requires_price_scenarios_and_layered_holding_strategy():
    required_snippets = [
        "1-3个月上行情景",
        "1-3个月下行情景",
        "6-12个月上行情景",
        "6-12个月下行情景",
        "#### 持仓策略：分层执行",
        "当前仓位处理",
        "加仓条件",
        "减仓/止损条件",
        "目标区间",
        "复查节奏",
    ]

    for snippet in required_snippets:
        assert snippet in POSITION_REVIEW_OUTPUT_FORMAT
        assert snippet in build_planning_system_prompt()


def test_entry_analysis_requires_actionable_entry_plan():
    required_snippets = [
        "Planning 摘要",
        "Execute 证据摘要",
        "可见计划，不暴露隐藏思维链",
        "证据账本摘要",
        "停止条件",
        "需要验证",
        "入场决策表格",
        "入场结论",
        "OPEN/WAIT/REJECT/MONITOR",
        "理想入场区间",
        "次优入场区间",
        "禁止追高线",
        "首仓比例",
        "加仓条件",
        "止损位",
        "第一目标位",
        "第二目标位",
        "淘汰条件",
        "分层建仓计划",
        "淘汰与复查条件",
        "风险收益比",
        "不得建议 OPEN",
        "如果主证据缺失，不得给 OPEN",
    ]

    for snippet in required_snippets:
        assert snippet in ENTRY_ANALYSIS_OUTPUT_FORMAT
        assert snippet in build_planning_system_prompt()


def test_watchlist_candidate_pool_protocol_defines_l1_boundaries_and_schema():
    prompt = build_planning_system_prompt()
    required_snippets = [
        "候选池只存在于 `watchlist_scan`",
        "`position_review`：禁止启动全市场候选池",
        "`entry_analysis`：如果用户已给出明确标的，只分析该标的",
        "\"expert_packets\"",
        "\"themes\"",
        "\"quality\"",
        "\"hard_exclusion\"",
        "\"capacity\"",
        "signal_score/final_score/score",
        "只表示 L1 入池召回强度",
        "不得当成买入推荐分",
        "`themes` 是主题/事件观察，不是股票候选",
        "`fallback_seed_pool`",
        "只能作为兜底观察池",
        "没有完成逐股深度分析的股票，只能显示为“观察池”",
        "系统注入给二阶段的候选池应使用以下压缩模板",
        "## 候选池（来自一阶段多专家发现）",
        "完整数据：candidate_discovery.json / candidate_pool_run_id",
        "一阶段只增删和合并候选池成员；二阶段不修改候选池成员",
    ]

    for snippet in required_snippets:
        assert snippet in CANDIDATE_POOL_PROTOCOL
        assert snippet in prompt


def test_watchlist_prompt_rules_keep_candidate_score_out_of_recommendation_score():
    prompt = build_planning_system_prompt()
    required_snippets = [
        "候选池分数必须命名为“入池分/召回分”",
        "不得写成“推荐分/买入分”",
        "不得把 L1 入池分、策略命中或主题观察当成买入依据",
        "未深度分析候选被包装为推荐",
        "候选池入池分只叫“入池分/召回分”",
        "表头必须叫“入池分”，禁止写“推荐分”",
        "未完成逐股深度分析的股票不得进入首选/次选/可买入区域",
        "如果全部候选都是 wait/monitor/reject",
        "暂无可入手标的",
    ]

    for snippet in required_snippets:
        assert snippet in prompt


def test_watchlist_scan_output_format_is_first_class_contract():
    prompt = build_planning_system_prompt()
    required_snippets = [
        "## 选股报告输出规范（watchlist_scan）",
        "严格按以下顺序输出",
        "1. 核心推荐结论",
        "2. 最终推荐表格",
        "3. 候选池概览",
        "4. 逐股深度摘要",
        "5. 组合配置与执行条件",
        "6. 反方审查摘要与 Judge 裁决",
        "| 最终动作 | OPEN_PARTIAL/WAIT_FOR_MORE_DATA/REJECT_ALL/MONITOR |",
        "| 股票 | 入池来源 | 入池分 | 是否深度分析 | 入池理由 | 观察状态 |",
        "| 股票 | 结论 | 入场条件 | 止损/淘汰 | 关键支持 | 主要反证/缺口 |",
        "| 股票 | 动作 | 首仓金额/比例 | 加仓条件 | 降级条件 | 复查时间 |",
        "如果全部候选都是 wait/monitor/reject",
        "首选标的` 必须写“暂无可入手标的”",
    ]

    for snippet in required_snippets:
        assert snippet in WATCHLIST_SCAN_OUTPUT_FORMAT
        assert snippet in prompt


def test_watchlist_debate_protocol_requires_structured_judge_fields():
    prompt = build_planning_system_prompt()
    required_snippets = [
        '"final_action": "open_partial | wait_for_more_data | reject_all | monitor"',
        '"accepted_candidates"',
        '"rejected_candidates"',
        '"wait_candidates"',
        '"portfolio_allocation_accepted"',
        '"allocation_adjustments"',
        '"risk_controls"',
        '"unresolved_conflicts"',
    ]

    for snippet in required_snippets:
        assert snippet in DEBATE_PROTOCOL
        assert snippet in prompt


def test_tool_budget_and_tool_policy_are_not_duplicate_watchlist_rules():
    prompt = build_planning_system_prompt()

    assert "预算值由 runner 根据 `AGENT_MAX_STEPS`" in EXECUTE_PROTOCOL
    assert "超过 12 次进入“工具节约阶段”" in EXECUTE_PROTOCOL
    assert "超过 16 次进入“关键预算阶段”" in EXECUTE_PROTOCOL
    assert "watchlist_scan 的候选池规则、触发边界、压缩注入格式和逐股取证要求见 `Watchlist Candidate Pool Protocol`" in TOOL_USE_POLICY
    assert "discover_watchlist_candidates` 只产生候选" not in TOOL_USE_POLICY
    assert "预算值由 runner 根据 `AGENT_MAX_STEPS`" in prompt


def test_english_prompt_is_marked_placeholder():
    assert "lightweight placeholder" in EN_SYSTEM_PROMPT
    assert "not the production" in EN_SYSTEM_PROMPT
    assert "Chinese prompt sections" in EN_SYSTEM_PROMPT


def test_global_output_and_scheduled_trigger_constraints_are_present():
    prompt = build_planning_system_prompt()
    required_snippets = [
        "系统调度任务触发",
        "不要在没有触发源时生成泛化日报",
        "最终用户可见报告默认不超过 3000 个中文字符",
        "工具未返回时不得凭术语补写",
    ]

    for snippet in required_snippets:
        assert snippet in prompt


def test_prompt_requires_realtime_quote_session_wording():
    required_snippets = [
        "market_session",
        "query_date",
        "quote_trade_date",
        "price_label",
        "change_pct_label",
        "freshness_note",
        "查询日休市/非交易日",
        "截至 quote_trade_date 的最新可用行情",
        "不得写“今日 +x%”",
        "最近交易日涨跌幅 +x%（查询日休市）",
    ]

    prompt = build_planning_system_prompt()
    for snippet in required_snippets:
        assert snippet in prompt


def test_constraints_keep_confidence_internal_only():
    assert "工具返回的 `confidence` 字段只允许用于内部判断数据可靠性" in CONSTRAINTS
    assert "禁止在最终用户输出中展示" in CONSTRAINTS
    assert "置信度 80%" in CONSTRAINTS


def test_planning_protocol_requires_actionable_planner_contract():
    prompt = build_planning_system_prompt()

    required_planning_rules = [
        "你是任务规划者，不是最终交易结论生成器",
        "Planner 必须先选择主维度，再选择辅助维度",
        "# todo",
        "## 任务识别",
        "## 维度计划",
        "## 工具计划",
        "## 反证检查",
        "不直接假设存在 `get_tools_for_capability`",
        '"tool_plan"',
        '"risk_checks"',
        "执行后复核",
    ]
    for rule in required_planning_rules:
        assert rule in prompt


def test_execute_protocol_requires_auditable_execution_contract():
    prompt = build_planning_system_prompt()
    required_execute_rules = [
        "执行器的职责不是重新规划",
        "Evidence Ledger",
        "`tool_execution_plan`",
        "每个工具结果都必须进入证据账本",
        "每轮工具返回后更新 `todo.md` 状态",
        "工具失败与降级",
        "Trace Artifacts",
        "`events.ndjson`",
        "`tool_calls.json`",
        "`evidence_ledger.json`",
        "`final.md`",
        "最终输出审计门槛",
    ]

    for rule in required_execute_rules:
        assert rule in EXECUTE_PROTOCOL
        assert rule in prompt


def test_debate_protocol_requires_forced_opposition_and_judge():
    prompt = build_planning_system_prompt()
    required_rules = [
        "强制反向立场辩论 + Judge 最终裁决",
        "Shared Evidence Bundle",
        "Adversarial Thesis Agent",
        "反方 Agent 不能调用新工具、不能编造数据",
        "双方必须给出自己的失效条件",
        "Judge 必须输出 `insufficient_data` 或 `no_trade`",
        "持仓模式 position_review",
        "选股模式 entry_analysis",
    ]

    for rule in required_rules:
        assert rule in DEBATE_PROTOCOL
        assert rule in prompt
