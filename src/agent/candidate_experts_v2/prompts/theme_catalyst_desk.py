# -*- coding: utf-8 -*-
"""System prompt for the theme-catalyst thesis desk (主题催化席)."""

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

- 催化真实：来自 `news_theme_daily` 原文主题、热点板块资金、明确产业链事件或当日快讯，而不是泛概念联想。
- 业务匹配：`business_context` 的行业/板块/主题线索与日报主题一致，允许宽口径产业链映射，但必须说明角色。
- 资金或板块有验证：板块资金净流入、热点板块龙头、个股资金回补、放量承接、热度上升至少命中一类。
- 负面/澄清一票否决：若公司新闻命中“不涉及/未开展/澄清/不属实/风险提示/减持/处罚/亏损”等，只能 rejected 或 neutral。

【setup 标注】所有 pick 标 setup_type=theme_catalyst。
【失败条件（每个 pick 必写）】主题热度退潮 / 板块资金转负 / 个股放量冲高回落 / 业务映射被证伪 / 盘前催化未被盘中成交验证。
【主要警惕】把海外巨头新闻硬映射到弱相关 A 股、把澄清公告当利好、追在情绪高潮末端。
"""


def build_theme_catalyst_desk_system_prompt(
    *,
    manifest: DimensionManifest | None = None,
    variables: Mapping[str, Any] | None = None,
) -> str:
    if manifest is None:
        manifest = load_manifest("theme_catalyst_desk")
    block = render_manifest_block(manifest, variables=variables)
    return SHARED_DESK_HEADER.format(
        desk_label="主题催化席",
        thesis_one_line="盘前/当日高质量主题催化与业务归属匹配，并已有资金或板块验证",
        tool_manifest_block=block,
        desk_specific_block=_DESK_SPECIFIC,
        setup_type_example="theme_catalyst",
    )


THEME_CATALYST_DESK_SYSTEM_PROMPT = build_theme_catalyst_desk_system_prompt()


__all__ = [
    "THEME_CATALYST_DESK_SYSTEM_PROMPT",
    "build_theme_catalyst_desk_system_prompt",
]
