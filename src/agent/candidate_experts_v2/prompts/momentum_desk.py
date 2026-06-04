# -*- coding: utf-8 -*-
"""System prompt for the momentum thesis desk (P4 trend-continuation + limit-up seat)."""

from __future__ import annotations

from typing import Any, Mapping

from src.agent.candidate_experts_v2.prompts._desk_base import SHARED_DESK_HEADER
from src.agent.candidate_experts_v2.prompts._renderer import render_manifest_block
from src.agent.candidate_experts_v2.tools_manifest import (
    DimensionManifest,
    load_manifest,
)

_DESK_SPECIFIC = """\
## 本席位覆盖两类打法（每个 pick 必须标注 setup_type）

**① trend_continuation 强势延续**
趋势结构健康、均线多头、有舒服回踩位、量价不透支。

**② capital_momentum 资金/连板**
资金承接真实非诱多——主力净流入有持续性 + 龙虎结构健康 + 封板强度够 + 连板高度可控（排除高位板 ≥5）。

## 看多标准

- 必做追高检查：乖离/RSI/近5日涨幅（看 FactSheet）过热 → 至多 watch，不得 support。
- 延续类必须给出"舒服回踩位"；连板类必须判断"封板强度/是否开板/连板高度"。
- 板块强（FactSheet.sector_strength=strong）可作助攻；若个股主因是板块补涨，reason 标 setup_subtype=theme_follow，并检查"龙头已涨/二线未涨 + 资金是否流向二线"。

【失败条件】跌破突破位/趋势线（延续） / 破封板成本或跌破首板低点（连板） / 板块走弱（补涨）。
【主要警惕】追高、拥挤、次日分歧、高位板诱多、开板。
"""


def build_momentum_desk_system_prompt(
    *,
    manifest: DimensionManifest | None = None,
    variables: Mapping[str, Any] | None = None,
) -> str:
    if manifest is None:
        manifest = load_manifest("momentum_desk")
    block = render_manifest_block(manifest, variables=variables)
    return SHARED_DESK_HEADER.format(
        desk_label="动量席",
        thesis_one_line="趋势健康或资金承接真实的强势股，顺势等条件入场而非追高",
        tool_manifest_block=block,
        desk_specific_block=_DESK_SPECIFIC,
        setup_type_example="trend_continuation",
    )


MOMENTUM_DESK_SYSTEM_PROMPT = build_momentum_desk_system_prompt()


__all__ = [
    "MOMENTUM_DESK_SYSTEM_PROMPT",
    "build_momentum_desk_system_prompt",
]
