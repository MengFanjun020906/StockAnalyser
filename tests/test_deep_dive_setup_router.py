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

from src.agent.stock_selection_prompts import (
    _DEEP_DIVE_OUTPUT_SCHEMA,
    _DEEP_DIVE_PLAYBOOKS,
    build_deep_dive_prompt,
    deep_dive_router,
)
from src.agent.stock_selection import (
    _deep_dive_setup_fields,
    _infer_setup_type,
)


# --- router precedence ---------------------------------------------------

def test_router_maps_known_setup_types():
    for setup in ("early_turn", "trend_continuation", "capital_momentum", "quality_repair", "theme_follow"):
        assert deep_dive_router(setup) == setup


def test_router_unknown_falls_back_to_unknown():
    assert deep_dive_router("nonsense") == "unknown"
    assert deep_dive_router(None) == "unknown"
    assert deep_dive_router("") == "unknown"


def test_router_subtype_theme_follow_takes_precedence():
    assert deep_dive_router("early_turn", "theme_follow") == "theme_follow"
    assert deep_dive_router("quality_repair", "theme_follow") == "theme_follow"
    assert deep_dive_router("capital_momentum", "THEME_FOLLOW") == "theme_follow"


# --- routed prompt content ----------------------------------------------

def _routed(setup_type, **extra):
    payload = {"stock_code": "600000", "stock_name": "X", "setup_router_enabled": True, "setup_type": setup_type}
    payload.update(extra)
    return build_deep_dive_prompt(payload)


def test_each_setup_injects_its_playbook_block():
    markers = {
        "early_turn": "低位启动 early_turn",
        "trend_continuation": "强势延续 trend_continuation",
        "capital_momentum": "资金/连板 capital_momentum",
        "quality_repair": "质量修复 quality_repair",
        "theme_follow": "题材补涨 theme_follow",
    }
    for setup, marker in markers.items():
        prompt = _routed(setup)
        assert marker in prompt, setup


def test_routed_prompt_keeps_output_schema_identical():
    prompt = _routed("early_turn")
    assert _DEEP_DIVE_OUTPUT_SCHEMA in prompt


def test_every_playbook_decouples_failure_condition_and_stop_loss():
    for key, block in _DEEP_DIVE_PLAYBOOKS.items():
        assert "stop_loss" in block, key
        assert "failure_condition" in block, key


def test_market_block_switches_by_market():
    assert "A股专属口径" in _routed("early_turn", market="cn")
    assert "港股专属口径" in _routed("early_turn", market="hk")
    assert "美股专属口径" in _routed("early_turn", market="us")


def test_reused_evidence_block_lists_available_context():
    prompt = _routed("early_turn", fact_sheet={"trend": "neutral"}, upstream_evidence={"d": []})
    assert "只补缺口" in prompt
    assert "fact_sheet" in prompt


def test_conflict_block_only_when_flags_present():
    with_conflict = _routed("capital_momentum", conflict_flags=["capital_outflow_vs_momentum"])
    assert "冲突待裁决" in with_conflict
    assert "capital_outflow_vs_momentum" in with_conflict
    without = _routed("capital_momentum")
    assert "冲突待裁决" not in without


# --- flag-off legacy path -------------------------------------------------

def test_flag_off_uses_legacy_prompt_without_playbook():
    legacy = build_deep_dive_prompt({"stock_code": "600000", "stock_name": "X"})
    assert "single_stock_deep_dive" in legacy
    assert "打法席位" not in legacy
    assert "A股专属口径" not in legacy
    # output schema still present and identical
    assert _DEEP_DIVE_OUTPUT_SCHEMA in legacy


# --- transitional heuristic ----------------------------------------------

def test_infer_setup_type_by_source():
    assert _infer_setup_type({"source": "low_base_structure"}) == "early_turn"
    assert _infer_setup_type({"source": "limit_up_pool"}) == "capital_momentum"
    assert _infer_setup_type({"source": "sector_theme"}) == "theme_follow"
    assert _infer_setup_type({"source": "fundamental_snapshot"}) == "quality_repair"
    assert _infer_setup_type({"source": "alphasift"}) == "trend_continuation"


def test_infer_setup_type_keyword_fallback_and_default():
    assert _infer_setup_type({"source": "user_watchlist", "strategy_tags": ["低位启动"]}) == "early_turn"
    assert _infer_setup_type({"source": "user_watchlist", "reason": "板块补涨二线"}) == "theme_follow"
    assert _infer_setup_type({"source": "fallback"}) == "unknown"


def test_infer_setup_type_explicit_wins_over_source():
    assert _infer_setup_type({"setup_type": "quality_repair", "source": "limit_up_pool"}) == "quality_repair"


def test_deep_dive_setup_fields_passes_optional_context_only_when_present():
    full = _deep_dive_setup_fields(
        {
            "source": "limit_up_pool",
            "market": "CN",
            "setup_subtype": "theme_follow",
            "fact_sheet": {"x": 1},
            "conflict_flags": ["a"],
            "llm_expert_evidence": {"d": []},
        },
        market="cn",
    )
    assert full["setup_router_enabled"] is True
    assert full["setup_type"] == "capital_momentum"
    assert full["market"] == "cn"
    assert full["setup_subtype"] == "theme_follow"
    assert full["fact_sheet"] == {"x": 1}
    assert full["conflict_flags"] == ["a"]
    assert full["upstream_evidence"] == {"d": []}

    minimal = _deep_dive_setup_fields(None, market="hk")
    assert minimal["setup_type"] == "unknown"
    assert minimal["market"] == "hk"
    assert "fact_sheet" not in minimal
    assert "setup_subtype" not in minimal
    assert "conflict_flags" not in minimal
