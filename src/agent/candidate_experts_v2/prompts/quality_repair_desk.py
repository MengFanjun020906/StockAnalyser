# -*- coding: utf-8 -*-
"""System prompt for the quality-repair thesis desk (P4 fundamental improvement seat)."""

from __future__ import annotations

from typing import Any, Mapping

from src.agent.candidate_experts_v2.prompts._desk_base import SHARED_DESK_HEADER
from src.agent.candidate_experts_v2.prompts._renderer import render_manifest_block
from src.agent.candidate_experts_v2.tools_manifest import (
    DimensionManifest,
    load_manifest,
)

_DESK_SPECIFIC = """\
## 看多标准

- 盈利质量改善或亏损收窄（forecast/express/financial_indicators 取证）。
- 估值未透支（daily_basic 的 PE/PB 分位不极端）。
- 价格滞后：基本面已改善但股价仍处中低位/未充分反映（对照 FactSheet 位置）。
- 行业景气向上更佳（可选，非必须）。

【setup 标注】所有 pick 标 setup_type=quality_repair。
【失败条件（每个 pick 必写）】基本面逻辑破坏（业绩证伪/景气逆转），而非纯技术止损；估值已透支则降级 watch。
【主要警惕】价值陷阱（便宜但持续恶化）、估值已透支、亏损但无改善证据。
"""


def build_quality_repair_desk_system_prompt(
    *,
    manifest: DimensionManifest | None = None,
    variables: Mapping[str, Any] | None = None,
) -> str:
    if manifest is None:
        manifest = load_manifest("quality_repair_desk")
    block = render_manifest_block(manifest, variables=variables)
    return SHARED_DESK_HEADER.format(
        desk_label="质量修复席",
        thesis_one_line="业绩/景气改善但价格尚未反映，估值未透支",
        tool_manifest_block=block,
        desk_specific_block=_DESK_SPECIFIC,
        setup_type_example="quality_repair",
    )


QUALITY_REPAIR_DESK_SYSTEM_PROMPT = build_quality_repair_desk_system_prompt()


__all__ = [
    "QUALITY_REPAIR_DESK_SYSTEM_PROMPT",
    "build_quality_repair_desk_system_prompt",
]
