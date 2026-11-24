# -*- coding: utf-8 -*-
"""Prompt version stamping (DOC-02a)."""

import re

from src.agent import planning_prompts, stock_selection_prompts
from src.agent.stock_selection import SelectionRunContext


def test_prompt_version_constants_are_dated():
    pattern = re.compile(r"^\d{4}-\d{2}-\d{2}$")
    assert pattern.match(stock_selection_prompts.PROMPT_VERSION)
    assert pattern.match(planning_prompts.PROMPT_VERSION)


def test_selection_run_context_records_prompt_versions():
    context = SelectionRunContext(run_id="run-1", user_message="看下自选股")

    payload = context.to_dict(include_full=False)

    assert payload["prompt_versions"] == {
        "stock_selection": stock_selection_prompts.PROMPT_VERSION,
        "planning": planning_prompts.PROMPT_VERSION,
    }


def test_prompt_version_is_not_injected_into_prompt_text():
    selection_text = stock_selection_prompts.build_candidate_discovery_prompt({})
    planning_text = planning_prompts.build_planning_system_prompt()

    assert stock_selection_prompts.PROMPT_VERSION not in selection_text
    assert planning_prompts.PROMPT_VERSION not in planning_text
