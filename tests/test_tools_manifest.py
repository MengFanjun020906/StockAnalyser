# -*- coding: utf-8 -*-
"""Tests for src.agent.candidate_experts_v2.tools_manifest.

Covers:
- YAML loader returns a valid DimensionManifest for the shipped desk manifests.
- Whitelist cross-check: extra manifest tool raises; missing-from-manifest warns.
- ToolRegistry cross-check: unknown tool name raises.
- typical_args param-name validation: unknown key raises; missing parameters
  metadata is silently tolerated.
- Placeholder substitution: {today} / {seed_codes} substituted; unknown keys
  preserved as literal `{key}`.
- Renderer: produces a Markdown block sorted by (priority, tool) with all
  required sections (何时调用 / 典型参数 / 返回概要 / 关键字段 / 组合提示 / 失败模式).
- build_early_turn_desk_system_prompt injects the manifest block into the template.
"""

from __future__ import annotations

import pytest

from src.agent.candidate_experts_v2.experts.early_turn_desk import EARLY_TURN_DESK_TOOLS
from src.agent.candidate_experts_v2.experts.theme_catalyst_desk import THEME_CATALYST_DESK_TOOLS
from src.agent.candidate_experts_v2.prompts._renderer import render_manifest_block
from src.agent.candidate_experts_v2.prompts.early_turn_desk import (
    build_early_turn_desk_system_prompt,
)
from src.agent.candidate_experts_v2.prompts.theme_catalyst_desk import (
    build_theme_catalyst_desk_system_prompt,
)
from src.agent.candidate_experts_v2.tools_manifest import (
    DimensionManifest,
    ManifestEntry,
    ManifestValidationError,
    load_manifest,
    render_typical_args,
    validate_manifest,
)


# ---------------------------------------------------------------------------
# Loader
# ---------------------------------------------------------------------------


def test_load_early_turn_desk_manifest_returns_validated_dimension_manifest():
    m = load_manifest("early_turn_desk")
    assert isinstance(m, DimensionManifest)
    assert m.dimension == "early_turn_desk"
    names = {e.tool for e in m.tools}
    assert names == set(EARLY_TURN_DESK_TOOLS)


def test_load_theme_catalyst_desk_manifest_returns_validated_dimension_manifest():
    m = load_manifest("theme_catalyst_desk")
    assert isinstance(m, DimensionManifest)
    assert m.dimension == "theme_catalyst_desk"
    names = {e.tool for e in m.tools}
    assert names == set(THEME_CATALYST_DESK_TOOLS)


def test_load_early_turn_desk_manifest_all_priorities_in_range():
    m = load_manifest("early_turn_desk")
    priorities = [e.priority for e in m.tools]
    assert all(1 <= p <= 99 for p in priorities)
    assert all(e.cost in {"cheap", "medium", "expensive"} for e in m.tools)


def test_load_missing_dimension_raises():
    with pytest.raises(FileNotFoundError):
        load_manifest("__nonexistent_dim__")


# ---------------------------------------------------------------------------
# validate_manifest cross-checks
# ---------------------------------------------------------------------------


def _entry(tool: str, **kwargs) -> ManifestEntry:
    return ManifestEntry(
        tool=tool,
        priority=kwargs.get("priority", 10),
        when_to_use="x",
        typical_args=kwargs.get("typical_args", {}),
        returns_summary="y",
        cost=kwargs.get("cost", "cheap"),
    )


def _manifest(entries):
    return DimensionManifest(dimension="early_turn_desk", tools=entries)


class _StubToolDef:
    def __init__(self, params):
        self.parameters = [type("P", (), {"name": n})() for n in params]


def test_validate_manifest_rejects_tool_outside_whitelist():
    m = _manifest([_entry("forbidden_tool")])
    with pytest.raises(ManifestValidationError) as exc:
        validate_manifest(
            m,
            whitelist=("get_tushare_moneyflow_ths",),
            tool_registry={"forbidden_tool": _StubToolDef(["stock_code"])},
        )
    assert "not in whitelist" in str(exc.value)


def test_validate_manifest_rejects_unknown_tool_in_registry():
    m = _manifest([_entry("get_tushare_moneyflow_ths")])
    with pytest.raises(ManifestValidationError) as exc:
        validate_manifest(
            m,
            whitelist=("get_tushare_moneyflow_ths",),
            tool_registry={},
        )
    assert "not registered in ToolRegistry" in str(exc.value)


def test_validate_manifest_rejects_unknown_typical_args_key():
    m = _manifest([_entry("t1", typical_args={"unknown_arg": 1})])
    with pytest.raises(ManifestValidationError) as exc:
        validate_manifest(
            m,
            whitelist=("t1",),
            tool_registry={"t1": _StubToolDef(["stock_code"])},
        )
    assert "typical_args keys not in its ToolDefinition parameters" in str(exc.value)


def test_validate_manifest_tolerates_missing_parameters_metadata():
    """Plain-callable registry entries (no `.parameters`) should skip param check."""
    m = _manifest([_entry("t1", typical_args={"anything": 1})])
    # Plain function: no .parameters attribute -> validation skips the check.
    validate_manifest(
        m,
        whitelist=("t1",),
        tool_registry={"t1": lambda **kw: None},
    )


def test_validate_manifest_warns_on_whitelist_tool_missing_from_manifest(caplog):
    m = _manifest([_entry("t1")])
    with caplog.at_level("WARNING"):
        validate_manifest(
            m,
            whitelist=("t1", "t2"),
            tool_registry={"t1": _StubToolDef([]), "t2": _StubToolDef([])},
        )
    assert any("missing entries for whitelist tools" in r.message for r in caplog.records)


def test_validate_full_early_turn_desk_manifest_against_real_registry():
    """The shipped early_turn_desk.yaml must validate against the real ToolRegistry."""
    from src.agent.tools.analysis_tools import ALL_ANALYSIS_TOOLS
    from src.agent.tools.data_tools import ALL_DATA_TOOLS
    from src.agent.tools.registry import ToolRegistry

    reg = ToolRegistry()
    for t in ALL_DATA_TOOLS + ALL_ANALYSIS_TOOLS:
        reg.register(t)

    m = load_manifest("early_turn_desk")
    validate_manifest(m, whitelist=EARLY_TURN_DESK_TOOLS, tool_registry=reg)


def test_validate_full_theme_catalyst_desk_manifest_against_real_registry():
    """The shipped theme_catalyst_desk.yaml must validate against the real ToolRegistry."""
    from src.agent.tools.analysis_tools import ALL_ANALYSIS_TOOLS
    from src.agent.tools.data_tools import ALL_DATA_TOOLS
    from src.agent.tools.registry import ToolRegistry
    from src.agent.tools.search_tools import ALL_SEARCH_TOOLS

    reg = ToolRegistry()
    for t in ALL_DATA_TOOLS + ALL_ANALYSIS_TOOLS + ALL_SEARCH_TOOLS:
        reg.register(t)

    m = load_manifest("theme_catalyst_desk")
    validate_manifest(m, whitelist=THEME_CATALYST_DESK_TOOLS, tool_registry=reg)


# ---------------------------------------------------------------------------
# Placeholder substitution
# ---------------------------------------------------------------------------


def test_render_typical_args_substitutes_known_placeholders():
    entry = _entry(
        "t",
        typical_args={"stock_code": "{seed_codes}", "trade_date": "{today}", "limit": 5},
    )
    out = render_typical_args(entry, {"today": "20260523", "seed_codes": "600000"})
    assert out == {"stock_code": "600000", "trade_date": "20260523", "limit": 5}


def test_render_typical_args_keeps_unknown_placeholders_literal():
    entry = _entry("t", typical_args={"x": "{unknown_key}"})
    out = render_typical_args(entry, {"today": "20260523"})
    assert out == {"x": "{unknown_key}"}


# ---------------------------------------------------------------------------
# Renderer
# ---------------------------------------------------------------------------


def test_render_manifest_block_orders_by_priority_then_tool():
    m = _manifest(
        [
            _entry("b_tool", priority=20),
            _entry("a_tool", priority=10),
            _entry("z_tool", priority=10),  # same priority, name tie-break
        ]
    )
    out = render_manifest_block(m)
    # a_tool (prio 10) before z_tool (prio 10) before b_tool (prio 20)
    assert out.index("a_tool") < out.index("z_tool") < out.index("b_tool")


def test_render_manifest_block_includes_all_required_sections():
    m = load_manifest("early_turn_desk")
    out = render_manifest_block(m, variables={"today": "20260523", "seed_codes": "600000"})
    assert "**何时调用**" in out
    assert "**典型参数**" in out
    assert "**返回概要**" in out
    assert "**关键字段**" in out
    assert "**组合提示**" in out
    assert "**失败模式**" in out
    # All early_turn_desk tools rendered
    for name in EARLY_TURN_DESK_TOOLS:
        assert f"`{name}`" in out


# ---------------------------------------------------------------------------
# build_early_turn_desk_system_prompt
# ---------------------------------------------------------------------------


def test_build_early_turn_desk_system_prompt_injects_manifest_block():
    prompt = build_early_turn_desk_system_prompt(
        variables={"today": "20260523", "seed_codes": "600519,000001"}
    )
    assert "## 工具手册" in prompt
    # Manifest block is substituted (no unresolved template placeholder)
    assert "{tool_manifest_block}" not in prompt
    # JSON output schema preserved
    assert '"candidates"' in prompt
    assert "## JSON response contract" in prompt
    assert "### Few-shot: 有合格候选" in prompt
    assert "### Few-shot: 没有合格候选" in prompt


def test_build_early_turn_desk_system_prompt_no_vars():
    prompt = build_early_turn_desk_system_prompt()
    # prompt builds without error even with no variables
    assert len(prompt) > 100


def test_build_theme_catalyst_desk_system_prompt_includes_catalyst_rules():
    prompt = build_theme_catalyst_desk_system_prompt(
        variables={"today": "20260602", "code": "000636", "name": "风华高科"}
    )
    assert "主题催化席" in prompt
    assert "news_theme_daily" in prompt
    assert "业务匹配" in prompt
    assert "setup_type=theme_catalyst" in prompt
    assert "`get_eastmoney_cjzc_daily`" in prompt
