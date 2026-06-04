import sys
from unittest.mock import MagicMock

try:
    import litellm  # noqa: F401
except ModuleNotFoundError:
    sys.modules["litellm"] = MagicMock()

from src.agent.candidate_experts_v2.schemas import FactSheet
from src.agent.candidate_experts_v2.veto_gate import apply_veto, veto_reasons


def _clean(code="600000"):
    return FactSheet(code=code)


# --- hard risk: always vetoes when enabled --------------------------------

def test_hard_risk_always_vetoes():
    sheet = FactSheet(code="600001", hard_risk_flags=["st"])
    kept, vetoed = apply_veto([sheet])
    assert kept == []
    assert len(vetoed) == 1
    assert vetoed[0]["code"] == "600001"
    assert any("hard_risk" in r for r in vetoed[0]["reasons"])


def test_clean_sheet_is_kept():
    sheet = _clean()
    kept, vetoed = apply_veto([sheet])
    assert kept == [sheet]
    assert vetoed == []


# --- soft red lines: only veto when the FactSheet bool is set -------------

def test_violent_outflow_bool_vetoes():
    sheet = FactSheet(code="600002", capital_violent_outflow=True)
    kept, vetoed = apply_veto([sheet])
    assert kept == []
    assert "capital_violent_outflow" in vetoed[0]["reasons"]


def test_breakdown_bool_vetoes():
    sheet = FactSheet(code="600003", breakdown_accelerating=True)
    kept, vetoed = apply_veto([sheet])
    assert "breakdown_accelerating" in vetoed[0]["reasons"]


def test_default_bools_false_keep_low_base_pick():
    # 低位票:资金 neutral/unknown,无红线 bool → 必须保留(核心非对称回归)
    sheet = FactSheet(code="600004", capital_direction="unknown", trend_state="neutral", range_pct_120=0.2)
    kept, vetoed = apply_veto([sheet])
    assert kept == [sheet]
    assert vetoed == []


# --- liquidity (opt-in) ---------------------------------------------------

def test_liquidity_not_enforced_by_default():
    sheet = FactSheet(code="600005", liquidity_ok=False)
    kept, vetoed = apply_veto([sheet])
    assert kept == [sheet]


def test_liquidity_enforced_when_requested():
    sheet = FactSheet(code="600005", liquidity_ok=False)
    kept, vetoed = apply_veto([sheet], enforce_liquidity=True)
    assert kept == []
    assert "liquidity_insufficient" in vetoed[0]["reasons"]


# --- gate disabled = no-op ------------------------------------------------

def test_gate_disabled_keeps_everything():
    bad = FactSheet(code="600006", hard_risk_flags=["delist"], capital_violent_outflow=True)
    kept, vetoed = apply_veto([bad], enabled=False)
    assert kept == [bad]
    assert vetoed == []


# --- wrapped items (dict / object carrying fact_sheet) --------------------

def test_dict_item_with_fact_sheet():
    keep = {"code": "600007", "fact_sheet": _clean("600007")}
    drop = {"code": "600008", "fact_sheet": FactSheet(code="600008", hard_risk_flags=["suspended"])}
    kept, vetoed = apply_veto([keep, drop])
    assert kept == [keep]
    assert vetoed[0]["code"] == "600008"


def test_dict_item_with_fact_sheet_as_dict():
    drop = {"code": "600009", "fact_sheet": {"code": "600009", "hard_risk_flags": ["st"]}}
    kept, vetoed = apply_veto([drop])
    assert kept == []
    assert vetoed[0]["code"] == "600009"


def test_unresolvable_item_is_kept():
    # cannot judge → never误杀
    item = {"code": "600010"}
    kept, vetoed = apply_veto([item])
    assert kept == [item]
    assert vetoed == []


# --- veto_reasons direct --------------------------------------------------

def test_veto_reasons_empty_for_clean():
    assert veto_reasons(_clean()) == []
