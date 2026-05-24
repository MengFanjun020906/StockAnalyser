# -*- coding: utf-8 -*-
"""Render a `DimensionManifest` into a Markdown tool-manual block.

Used by expert prompts to inject per-tool business semantics (when_to_use,
typical_args, returns_summary, combo_hints, failure_modes) into the SYSTEM
prompt so the LLM actually knows how to use each whitelisted tool.

Output shape (one block per tool, sorted by `priority` ascending):

    ### `<tool>` (priority N, cost: cheap)
    - **何时调用**: ...
    - **典型参数**: `{"stock_code": "600519", ...}`
    - **返回概要**: ...
    - **关键字段**: `f1`, `f2`
    - **组合提示**:
      - hint 1
      - hint 2
    - **失败模式**:
      - mode 1

`render_typical_args()` from tools_manifest substitutes `{today}` /
`{seed_codes}` placeholders at prompt build time.
"""

from __future__ import annotations

import json
from typing import Any, Mapping

from src.agent.candidate_experts_v2.tools_manifest import (
    DimensionManifest,
    render_typical_args,
)


def render_manifest_block(
    manifest: DimensionManifest,
    *,
    variables: Mapping[str, Any] | None = None,
) -> str:
    """Render a manifest into a Markdown tool-manual block.

    Args:
        manifest: validated DimensionManifest
        variables: runtime values for typical_args placeholder substitution.
            Common keys: `today` (YYYYMMDD), `seed_codes` (comma-joined codes).
            Missing keys are left as literal `{key}` in the rendered prompt so
            the model can still read the placeholder name.

    Returns:
        A multi-line Markdown string. The caller is responsible for placing it
        under a top-level heading (e.g. `## 工具手册`).
    """
    variables = variables or {}
    sorted_tools = sorted(manifest.tools, key=lambda e: (e.priority, e.tool))

    lines: list[str] = []
    for entry in sorted_tools:
        rendered_args = render_typical_args(entry, variables)
        try:
            args_json = json.dumps(rendered_args, ensure_ascii=False, sort_keys=True)
        except Exception:
            args_json = str(rendered_args)

        lines.append(f"### `{entry.tool}` (priority {entry.priority}, cost: {entry.cost})")
        lines.append(f"- **何时调用**: {entry.when_to_use.strip()}")
        lines.append(f"- **典型参数**: `{args_json}`")
        lines.append(f"- **返回概要**: {entry.returns_summary.strip()}")

        if entry.key_fields:
            joined = ", ".join(f"`{f}`" for f in entry.key_fields)
            lines.append(f"- **关键字段**: {joined}")

        if entry.combo_hints:
            lines.append("- **组合提示**:")
            for hint in entry.combo_hints:
                lines.append(f"  - {hint}")

        if entry.failure_modes:
            lines.append("- **失败模式**:")
            for mode in entry.failure_modes:
                lines.append(f"  - {mode}")

        lines.append("")  # blank line between tools

    return "\n".join(lines).rstrip()


__all__ = ["render_manifest_block"]
