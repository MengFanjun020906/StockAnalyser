# -*- coding: utf-8 -*-
"""Tests for the offline Agent infrastructure audit."""

from pathlib import Path

import importlib
import warnings
from typing import Any

from scripts.audit_agent_infra import (
    Finding,
    _audit_loop_files,
    _audit_planner,
    _audit_tools,
    _audit_trace_contracts,
    run_audit,
)


def test_agent_infra_audit_passes_current_contracts():
    result = run_audit()

    errors = [finding for finding in result["findings"] if finding["severity"] == "error"]
    assert errors == []
    assert result["summary"]["tool_count"] >= 70
    assert result["summary"]["planner_capability_count"] >= 10
    assert result["summary"]["planner_context_count"] >= 3


def test_tool_audit_flags_duplicate_invalid_schema_and_generic_profile():
    tool = _DummyTool(
        "unknown_audit_tool",
        parameters=[_DummyParameter("payload", "bogus", "")],
        schema_properties={},
    )
    findings: list[Finding] = []

    _audit_tools({"data": [tool], "search": [tool]}, _DummyRegistry([tool]), findings)

    messages = [finding.message for finding in findings if finding.severity == "error"]
    assert "Duplicate tool names would be overwritten in registry" in messages
    assert "Tool parameter missing from JSON schema" in messages
    assert "Tool parameter has invalid JSON schema type" in messages
    assert "Registered tools missing explicit ETL profiles" in messages


def test_planner_audit_flags_missing_mapped_tool(monkeypatch):
    from src.agent import planner
    from src.agent.factory import get_tool_registry

    missing_tool = "__missing_audit_tool__"
    monkeypatch.setitem(planner.CAPABILITY_TOOL_MAP, "__audit_capability__", [missing_tool])
    for map_name in (
        "CAPABILITY_PURPOSES",
        "CAPABILITY_EXPECTED_RESULTS",
        "CAPABILITY_DOWNSTREAM_USES",
        "CAPABILITY_FAILURE_FALLBACKS",
    ):
        monkeypatch.setitem(getattr(planner, map_name), "__audit_capability__", "audit metadata")

    registry = get_tool_registry()
    registry_names = {tool.name for tool in registry.list_tools()}
    findings: list[Finding] = []

    _audit_planner(registry, registry_names, findings)

    assert any(
        finding.severity == "error"
        and finding.area == "planner"
        and missing_tool in finding.detail.get("tools", [])
        for finding in findings
    )


def test_trace_audit_flags_missing_todo_contract_markers(monkeypatch):
    from src.agent.factory import get_tool_registry

    with warnings.catch_warnings():
        warnings.filterwarnings("ignore", category=DeprecationWarning)
        agent_endpoint = importlib.import_module("api.v1.endpoints.agent")

    monkeypatch.setattr(agent_endpoint, "_planner_to_todo_md", lambda *args, **kwargs: "# todo\n")
    findings: list[Finding] = []

    _audit_trace_contracts(get_tool_registry(), findings)

    assert any(
        finding.severity == "error"
        and finding.area == "trace"
        and "todo.md contract" in finding.message
        for finding in findings
    )


def test_loop_file_audit_flags_stale_state(tmp_path: Path):
    _write_minimal_loop_files(tmp_path)
    (tmp_path / "STATE.md").write_text(
        "# Loop State\n\n"
        "- Graphiti plan implementation is in final validation/PR handoff\n"
        "- Push `dev` and update PR #8 after final backend gate\n",
        encoding="utf-8",
    )
    findings: list[Finding] = []

    _audit_loop_files(tmp_path, findings)

    assert any(finding.severity == "error" and finding.area == "loop_state" for finding in findings)


def test_loop_file_audit_requires_autonomous_goal_budget(tmp_path: Path):
    _write_minimal_loop_files(tmp_path)
    (tmp_path / "loop-budget.md").write_text("# Budget\n\nDaily Triage only\n", encoding="utf-8")
    findings: list[Finding] = []

    _audit_loop_files(tmp_path, findings)

    assert any(finding.severity == "error" and finding.area == "loop_budget" for finding in findings)


def _write_minimal_loop_files(root: Path) -> None:
    (root / "LOOP.md").write_text(
        "# Loop\n\n"
        "## Autonomous Goal Loop Contract\n"
        "AGENTS.md remains the repository-wide hard baseline.\n",
        encoding="utf-8",
    )
    (root / "STATE.md").write_text("# State\n", encoding="utf-8")
    (root / "loop-budget.md").write_text("# Budget\n\nAutonomous Goal Loop checkpoint every 200k tokens\n", encoding="utf-8")
    (root / "loop-constraints.md").write_text(
        "# Constraints\n\n"
        "- Autonomous loop mode: push dev.\n"
        "- Fallback success is degraded success.\n"
        "- AGENTS.md remains the repository-wide hard baseline.\n",
        encoding="utf-8",
    )


class _DummyParameter:
    def __init__(self, name: str, param_type: str, description: str) -> None:
        self.name = name
        self.type = param_type
        self.description = description


class _DummyTool:
    def __init__(
        self,
        name: str,
        *,
        parameters: list[_DummyParameter] | None = None,
        schema_properties: dict[str, Any] | None = None,
    ) -> None:
        self.name = name
        self.description = "Dummy audit tool"
        self.parameters = parameters or []
        self._schema_properties = schema_properties or {}

    def to_openai_tool(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": self._schema_properties,
                },
            },
        }


class _DummyRegistry:
    def __init__(self, tools: list[_DummyTool]) -> None:
        self._tools = tools

    def list_tools(self) -> list[_DummyTool]:
        return list(self._tools)
