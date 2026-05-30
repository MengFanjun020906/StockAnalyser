# -*- coding: utf-8 -*-
"""看空红线否决门 (选股链路重构 P2).

非对称共识的「否决」侧:红线只看 FactSheet 的确定性事实,与席位无关,**只否决不加分**。
默认只有 ``hard_risk_flags`` 非空会触发(ST/退市/停牌/重大未澄清利空 —— 这条确定无需阈值)。
软红线 ``capital_violent_outflow`` / ``breakdown_accelerating`` 由 FactSheet 在阈值留空时
保持 False,因此默认永不触发(避免误杀干净低吸点)。可选 ``liquidity_ok=False`` 否决。

结果确定性、可审计:返回 (kept, vetoed),vetoed 每条带 code + reasons,不进 LLM。
回滚:``enabled=False`` → 全部放行。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from src.agent.candidate_experts_v2.schemas import FactSheet


def _coerce_fact_sheet(item: Any) -> Optional[FactSheet]:
    if isinstance(item, FactSheet):
        return item
    if isinstance(item, dict):
        fs = item.get("fact_sheet")
        if isinstance(fs, FactSheet):
            return fs
        if isinstance(fs, dict):
            try:
                return FactSheet(**fs)
            except Exception:
                return None
    fs_attr = getattr(item, "fact_sheet", None)
    if isinstance(fs_attr, FactSheet):
        return fs_attr
    return None


def veto_reasons(sheet: FactSheet, *, enforce_liquidity: bool = False) -> List[str]:
    """Deterministic red-line reasons for one FactSheet (empty → keep)."""
    reasons: List[str] = []
    if sheet.hard_risk_flags:
        reasons.append("hard_risk:" + ",".join(str(f) for f in sheet.hard_risk_flags))
    if sheet.capital_violent_outflow:
        reasons.append("capital_violent_outflow")
    if sheet.breakdown_accelerating:
        reasons.append("breakdown_accelerating")
    if enforce_liquidity and sheet.liquidity_ok is False:
        reasons.append("liquidity_insufficient")
    return reasons


def apply_veto(
    items: List[Any],
    *,
    enabled: bool = True,
    enforce_liquidity: bool = False,
) -> Tuple[List[Any], List[Dict[str, Any]]]:
    """Split items into (kept, vetoed) by shared bearish red lines.

    ``items`` may be FactSheet objects, or any object/dict carrying a ``fact_sheet``.
    When ``enabled`` is False the gate is a no-op (returns all items, no vetoes).
    Items without a resolvable FactSheet are kept (cannot judge → never误杀).
    """
    if not enabled:
        return list(items), []

    kept: List[Any] = []
    vetoed: List[Dict[str, Any]] = []
    for item in items:
        sheet = _coerce_fact_sheet(item)
        if sheet is None:
            kept.append(item)
            continue
        reasons = veto_reasons(sheet, enforce_liquidity=enforce_liquidity)
        if reasons:
            vetoed.append({"code": sheet.code, "reasons": reasons})
        else:
            kept.append(item)
    return kept, vetoed
