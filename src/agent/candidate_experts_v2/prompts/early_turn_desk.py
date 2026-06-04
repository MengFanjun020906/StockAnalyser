# -*- coding: utf-8 -*-
"""System prompt for the early-turn thesis desk (P4 low-base breakout seat)."""

from __future__ import annotations

from typing import Any, Mapping

from src.agent.candidate_experts_v2.prompts._desk_base import SHARED_DESK_HEADER
from src.agent.candidate_experts_v2.prompts._renderer import render_manifest_block
from src.agent.candidate_experts_v2.tools_manifest import (
    DimensionManifest,
    load_manifest,
)

_DESK_SPECIFIC = """\
## 看多标准（全部要点尽量满足，缺一要在 reason 说明）

- 位置仍低：range_pct_120 处中低位，距长期压力位有空间，不是连续大涨后的高位拥挤。
- 已出现转强拐点（至少一条，且要可复核）：首次站回关键均线 / 首次放量突破短平台 / 回踩不破后重新放量 / 主力资金由负转正。
- 不是弱势反弹：不能每次冲高都长上影大幅回落、不能无量、不能毫无结构改善。
- 多次试探同一压力位但回落收敛、下方有横盘换手 = 加分（吸筹）；反复冲高失败长上影 = 否决。

【setup 标注】所有 pick 标 setup_type=early_turn。
【失败条件（每个 pick 必写）】跌回平台下沿 / 放量失败后缩回 / 资金回流仅一天无持续 / 板块与个股拐点不共振。
【主要警惕】抄在下跌中继、假突破。低位本身不构成理由——必须"低位证据 + 转强证据 + 失败条件"三者齐备。
"""


def build_early_turn_desk_system_prompt(
    *,
    manifest: DimensionManifest | None = None,
    variables: Mapping[str, Any] | None = None,
) -> str:
    if manifest is None:
        manifest = load_manifest("early_turn_desk")
    block = render_manifest_block(manifest, variables=variables)
    return SHARED_DESK_HEADER.format(
        desk_label="低位启动席",
        thesis_one_line="跌透了、刚出现转强拐点、位置仍低且未透支",
        tool_manifest_block=block,
        desk_specific_block=_DESK_SPECIFIC,
        setup_type_example="early_turn",
    )


EARLY_TURN_DESK_SYSTEM_PROMPT = build_early_turn_desk_system_prompt()


__all__ = [
    "EARLY_TURN_DESK_SYSTEM_PROMPT",
    "build_early_turn_desk_system_prompt",
]
