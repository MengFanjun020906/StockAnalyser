from src.agent.planning_prompts import (
    CONSTRAINTS,
    EVENT_TRIGGER_POLICY,
    POSITION_REVIEW_OUTPUT_FORMAT,
    TOOL_USE_POLICY,
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
    assert len(get_default_prompt_sections()) == 12


def test_default_zh_prompt_contains_phase_one_contract_sections():
    prompt = build_planning_system_prompt()

    required_snippets = [
        "你是 StockAnalyser Agent",
        "## 分析维度与能力域",
        "## Planning -> Execute 协议",
        "### Planner 角色边界",
        "### todo.md 风格计划格式",
        "### 工具计划规范",
        "### 结构化执行计划",
        "## 约束规则",
        "## 账户感知规则",
        "## 重大事件触发规则",
        "## 持仓报告输出规范（position_review）",
        "持仓动作表格",
        "执行动作矩阵",
    ]
    for snippet in required_snippets:
        assert snippet in prompt


def test_prompt_options_can_remove_optional_policy_sections():
    prompt = build_planning_system_prompt(
        PromptBuildOptions(include_tool_policy=False, include_event_policy=False)
    )

    assert TOOL_USE_POLICY not in prompt
    assert EVENT_TRIGGER_POLICY not in prompt
    assert CONSTRAINTS in prompt
    assert POSITION_REVIEW_OUTPUT_FORMAT in prompt


def test_prompt_extra_instructions_are_appended_without_dropping_contract():
    prompt = build_zh_planning_system_prompt(extra_instructions="只分析用户指定股票。")

    assert "## 额外指令" in prompt
    assert "只分析用户指定股票。" in prompt
    assert "## 约束规则" in prompt
    assert "## 持仓报告输出规范（position_review）" in prompt


def test_position_review_output_does_not_expose_confidence_field():
    forbidden_public_fields = [
        "| 置信度 |",
        '"confidence"',
        "confidence_level",
    ]

    for forbidden in forbidden_public_fields:
        assert forbidden not in POSITION_REVIEW_OUTPUT_FORMAT
        assert forbidden not in build_planning_system_prompt()


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
