# -*- coding: utf-8 -*-
"""System prompts for v2 candidate expert committee."""

from src.agent.candidate_experts_v2.prompts.early_turn_desk import (
    EARLY_TURN_DESK_SYSTEM_PROMPT,
    build_early_turn_desk_system_prompt,
)
from src.agent.candidate_experts_v2.prompts.momentum_desk import (
    MOMENTUM_DESK_SYSTEM_PROMPT,
    build_momentum_desk_system_prompt,
)
from src.agent.candidate_experts_v2.prompts.quality_repair_desk import (
    QUALITY_REPAIR_DESK_SYSTEM_PROMPT,
    build_quality_repair_desk_system_prompt,
)

__all__ = [
    "EARLY_TURN_DESK_SYSTEM_PROMPT",
    "build_early_turn_desk_system_prompt",
    "MOMENTUM_DESK_SYSTEM_PROMPT",
    "build_momentum_desk_system_prompt",
    "QUALITY_REPAIR_DESK_SYSTEM_PROMPT",
    "build_quality_repair_desk_system_prompt",
]
