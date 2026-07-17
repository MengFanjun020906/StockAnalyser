#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Offline audit for foundational Agent infrastructure contracts.

The audit is intentionally deterministic: it does not call LLMs, network
providers, databases, or external services.  It verifies that local contracts
for tool registration, planner metadata, trace artifacts, and loop state stay
in sync.
"""

from __future__ import annotations

import argparse
import importlib
import json
import os
import sys
import warnings
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")

VALID_JSON_SCHEMA_TYPES = {"string", "number", "integer", "boolean", "array", "object"}

TOOL_GROUP_IMPORTS: Sequence[Tuple[str, str, str]] = (
    ("data", "src.agent.tools.data_tools", "ALL_DATA_TOOLS"),
    ("analysis", "src.agent.tools.analysis_tools", "ALL_ANALYSIS_TOOLS"),
    ("search", "src.agent.tools.search_tools", "ALL_SEARCH_TOOLS"),
    ("market", "src.agent.tools.market_tools", "ALL_MARKET_TOOLS"),
    ("backtest", "src.agent.tools.backtest_tools", "ALL_BACKTEST_TOOLS"),
    ("graph", "src.agent.tools.graph_tools", "ALL_GRAPH_TOOLS"),
)

PLANNER_METADATA_MAPS = (
    "CAPABILITY_PURPOSES",
    "CAPABILITY_EXPECTED_RESULTS",
    "CAPABILITY_DOWNSTREAM_USES",
    "CAPABILITY_FAILURE_FALLBACKS",
)

REQUIRED_TODO_MARKERS = (
    "expected_result=",
    "downstream_use=",
    "fallback_on_failure=",
    "next_step=",
    "## Planning Ledger",
    "reuse_payload=",
    "invalidates_on=",
    "## Replan",
    "## Execute Protocol",
)

REQUIRED_LOOP_FILES = ("LOOP.md", "STATE.md", "loop-budget.md", "loop-constraints.md")

AGENT_OPERATING_DOC = Path("docs/architecture/agent-loop-workflow-glossary.md")

REQUIRED_README_MARKERS = (
    "AI Operating Model",
    "Serenity Investment Orchestrator / 静研投资编排官",
    "agent-loop-workflow-glossary.md",
    "entry_execution_backtest.jsonl",
)

REQUIRED_AGENT_OPERATING_DOC_MARKERS = (
    "Planning Ledger / 计划账本",
    "Workflow / 工作流",
    "Loop / 循环",
    "Serenity Investment Orchestrator / 静研投资编排官",
    "Structural Reversal Desk / 结构反转席",
    "Theme Catalyst Desk / 主题催化席",
)

STALE_STATE_PATTERNS = (
    "Graphiti plan implementation is in final validation/PR handoff",
    "Push `dev` and update PR #8 after final backend gate",
)


@dataclass(frozen=True)
class Finding:
    """One audit finding."""

    severity: str
    area: str
    message: str
    detail: Dict[str, Any] = field(default_factory=dict)


def run_audit(root: Optional[Path] = None) -> Dict[str, Any]:
    """Run all offline infrastructure checks and return a JSON-safe payload."""
    repo_root = Path(root or REPO_ROOT)
    findings: List[Finding] = []

    tool_groups = _load_declared_tool_groups(findings)
    registry = _load_tool_registry(findings)
    registry_names = _audit_tools(tool_groups, registry, findings)

    planner_summary = _audit_planner(registry, registry_names, findings)
    _audit_trace_contracts(registry, findings)
    _audit_loop_files(repo_root, findings)
    _audit_agent_operating_docs(repo_root, findings)

    severity_counts = Counter(finding.severity for finding in findings)
    summary = {
        "tool_count": len(registry_names),
        "tool_group_count": len(tool_groups),
        "planner_capability_count": planner_summary.get("capability_count", 0),
        "planner_context_count": planner_summary.get("context_count", 0),
        "error_count": severity_counts.get("error", 0),
        "warning_count": severity_counts.get("warning", 0),
    }

    return {
        "schema_version": 1,
        "summary": summary,
        "findings": [asdict(finding) for finding in findings],
    }


def _load_declared_tool_groups(findings: List[Finding]) -> Dict[str, List[Any]]:
    groups: Dict[str, List[Any]] = {}
    for group_name, module_name, attr_name in TOOL_GROUP_IMPORTS:
        try:
            module = importlib.import_module(module_name)
            value = getattr(module, attr_name)
        except Exception as exc:  # pragma: no cover - protects CLI diagnostics
            _add(findings, "error", "tools", f"Failed to import tool group {group_name}", {"error": str(exc)})
            groups[group_name] = []
            continue
        groups[group_name] = list(value or [])
    return groups


def _load_tool_registry(findings: List[Finding]) -> Any:
    try:
        from src.agent.factory import get_tool_registry

        return get_tool_registry()
    except Exception as exc:  # pragma: no cover - protects CLI diagnostics
        _add(findings, "error", "tools", "Failed to load ToolRegistry", {"error": str(exc)})
        return _EmptyRegistry()


def _audit_tools(
    tool_groups: Mapping[str, Sequence[Any]],
    registry: Any,
    findings: List[Finding],
) -> set[str]:
    declared: Dict[str, List[str]] = defaultdict(list)
    for group_name, tools in tool_groups.items():
        for tool in tools:
            name = str(getattr(tool, "name", "") or "").strip()
            if not name:
                _add(findings, "error", "tools", "Tool without a name", {"group": group_name})
                continue
            declared[name].append(group_name)

    duplicates = {
        name: groups
        for name, groups in declared.items()
        if len(groups) > 1
    }
    if duplicates:
        _add(findings, "error", "tools", "Duplicate tool names would be overwritten in registry", duplicates)

    registry_tools = list(registry.list_tools()) if hasattr(registry, "list_tools") else []
    registry_names = {str(getattr(tool, "name", "") or "") for tool in registry_tools}
    declared_names = set(declared)

    missing_from_registry = sorted(declared_names - registry_names)
    if missing_from_registry:
        _add(findings, "error", "tools", "Declared tools missing from ToolRegistry", {"tools": missing_from_registry})

    extra_registry_tools = sorted(registry_names - declared_names)
    if extra_registry_tools:
        _add(findings, "warning", "tools", "ToolRegistry has tools outside declared ALL_* groups", {"tools": extra_registry_tools})

    _audit_tool_schemas(registry_tools, findings)
    _audit_tool_etl_profiles(registry_names, findings)
    return registry_names


def _audit_tool_schemas(tools: Sequence[Any], findings: List[Finding]) -> None:
    for tool in tools:
        name = str(getattr(tool, "name", "") or "")
        description = str(getattr(tool, "description", "") or "").strip()
        if not description:
            _add(findings, "warning", "tool_schema", "Tool description is empty", {"tool": name})

        try:
            openai_tool = tool.to_openai_tool()
            params = openai_tool["function"]["parameters"]
            properties = params.get("properties") or {}
        except Exception as exc:
            _add(findings, "error", "tool_schema", "Tool cannot render OpenAI schema", {"tool": name, "error": str(exc)})
            continue

        for parameter in getattr(tool, "parameters", []) or []:
            param_name = str(getattr(parameter, "name", "") or "").strip()
            param_type = str(getattr(parameter, "type", "") or "").strip()
            param_description = str(getattr(parameter, "description", "") or "").strip()
            if not param_name:
                _add(findings, "error", "tool_schema", "Tool parameter without a name", {"tool": name})
                continue
            if param_name not in properties:
                _add(findings, "error", "tool_schema", "Tool parameter missing from JSON schema", {"tool": name, "parameter": param_name})
            if param_type not in VALID_JSON_SCHEMA_TYPES:
                _add(findings, "error", "tool_schema", "Tool parameter has invalid JSON schema type", {"tool": name, "parameter": param_name, "type": param_type})
            if not param_description:
                _add(findings, "warning", "tool_schema", "Tool parameter description is empty", {"tool": name, "parameter": param_name})


def _audit_tool_etl_profiles(registry_names: Iterable[str], findings: List[Finding]) -> None:
    try:
        from src.agent.runner import resolve_tool_etl_profile
    except Exception as exc:  # pragma: no cover - protects CLI diagnostics
        _add(findings, "error", "tools", "Failed to import ETL profile resolver", {"error": str(exc)})
        return

    generic = sorted(name for name in registry_names if resolve_tool_etl_profile(name) == "generic")
    if generic:
        _add(findings, "error", "tools", "Registered tools missing explicit ETL profiles", {"tools": generic})


def _audit_planner(registry: Any, registry_names: set[str], findings: List[Finding]) -> Dict[str, int]:
    try:
        from src.agent import planner
        from src.schemas.agent_context import AgentUserContext, PositionContext, ReportContext
    except Exception as exc:  # pragma: no cover - protects CLI diagnostics
        _add(findings, "error", "planner", "Failed to import planner contracts", {"error": str(exc)})
        return {"capability_count": 0, "context_count": 0}

    capability_map = dict(getattr(planner, "CAPABILITY_TOOL_MAP", {}))
    capability_keys = set(capability_map)
    for map_name in PLANNER_METADATA_MAPS:
        metadata = dict(getattr(planner, map_name, {}))
        missing = sorted(capability_keys - set(metadata))
        extra = sorted(set(metadata) - capability_keys)
        if missing or extra:
            _add(
                findings,
                "error",
                "planner",
                "Planner capability metadata is not in lockstep",
                {"map": map_name, "missing": missing, "extra": extra},
            )

    missing_tools = sorted({
        tool_name
        for tool_names in capability_map.values()
        for tool_name in tool_names
        if tool_name not in registry_names
    })
    if missing_tools:
        _add(findings, "error", "planner", "Planner maps capabilities to missing tools", {"tools": missing_tools})

    contexts = {
        "entry_analysis": AgentUserContext(
            report=ReportContext(
                intent="entry_analysis",
                analysis_mode="planning_execute",
                primary_symbol="600519",
                target_symbols=["600519"],
            ),
        ),
        "position_review": AgentUserContext(
            positions=[PositionContext(symbol="600519", quantity=100, avg_cost=1500)],
            report=ReportContext(
                intent="position_review",
                analysis_mode="planning_execute",
                primary_symbol="600519",
                target_symbols=["600519"],
            ),
        ),
        "watchlist_scan": AgentUserContext(
            report=ReportContext(
                intent="watchlist_scan",
                analysis_mode="planning_execute",
                target_symbols=["600519", "000001"],
            ),
        ),
    }

    plans: Dict[str, Dict[str, Any]] = {}
    for label, context in contexts.items():
        plan = planner.build_planning_result(context, tool_registry=registry).to_dict()
        plans[label] = plan
        if not plan.get("capabilities"):
            _add(findings, "error", "planner", "Planner produced no capabilities", {"context": label})
        if not plan.get("required_tools"):
            _add(findings, "error", "planner", "Planner produced no required tools", {"context": label})
        for item in plan.get("tool_execution_plan") or []:
            missing_fields = [
                field_name
                for field_name in ("purpose", "expected_result", "downstream_use", "fallback_on_failure", "next_step")
                if not str(item.get(field_name) or "").strip()
            ]
            if missing_fields:
                _add(findings, "error", "planner", "Planner tool step misses handoff fields", {"context": label, "capability": item.get("capability"), "fields": missing_fields})

    if "portfolio_context" not in plans["position_review"].get("capabilities", []):
        _add(findings, "error", "planner", "Position review did not include portfolio context")
    if "watchlist_discovery" not in plans["watchlist_scan"].get("capabilities", []):
        _add(findings, "error", "planner", "Watchlist scan did not include watchlist discovery")
    if len({tuple(plan.get("required_tools") or []) for plan in plans.values()}) != len(plans):
        _add(findings, "error", "planner", "Representative contexts produced identical tool plans")

    return {"capability_count": len(capability_keys), "context_count": len(contexts)}


def _audit_trace_contracts(registry: Any, findings: List[Finding]) -> None:
    try:
        with warnings.catch_warnings():
            warnings.filterwarnings("ignore", category=DeprecationWarning)
            from api.v1.endpoints.agent import _build_evidence_ledger, _planner_to_todo_md
        from src.agent.planner import build_planning_result
        from src.schemas.agent_context import AgentUserContext, ReportContext
    except Exception as exc:  # pragma: no cover - protects CLI diagnostics
        _add(findings, "error", "trace", "Failed to import trace artifact contracts", {"error": str(exc)})
        return

    context = AgentUserContext(
        report=ReportContext(
            intent="entry_analysis",
            analysis_mode="planning_execute",
            primary_symbol="600519",
            target_symbols=["600519"],
        ),
    )
    planner_payload = build_planning_result(context, tool_registry=registry).to_dict()
    first_tool = (planner_payload.get("required_tools") or ["get_realtime_quote"])[0]
    tool_calls = [
        {
            "tool": first_tool,
            "arguments": {"stock_code": "600519"},
            "success": True,
            "duration": 0.01,
            "result_length": 32,
            "result_preview": json.dumps({"status": "ok", "price": 100}, ensure_ascii=False),
        },
        {
            "tool": "search_stock_news",
            "arguments": {"stock_code": "600519"},
            "success": False,
            "duration": 0.02,
            "result_length": 20,
            "result_preview": json.dumps({"status": "failed", "error": "offline"}, ensure_ascii=False),
        },
    ]

    todo = _planner_to_todo_md(planner_payload, {"account_count": 0, "position_count": 0}, tool_calls=tool_calls)
    missing_markers = [marker for marker in REQUIRED_TODO_MARKERS if marker not in todo]
    if missing_markers:
        _add(findings, "error", "trace", "todo.md contract misses required planner/execute markers", {"missing": missing_markers})

    if "planned_capability=" not in todo:
        _add(findings, "error", "trace", "Executed tool status is not linked back to planned capability")

    ledger = _build_evidence_ledger(tool_calls)
    entries = ledger.get("entries") or []
    statuses = [entry.get("status") for entry in entries]
    if ledger.get("entry_count") != 2 or "success" not in statuses or "failed" not in statuses:
        _add(findings, "error", "trace", "Evidence ledger did not preserve success/failure statuses", {"ledger": ledger})
    for entry in entries:
        if not entry.get("limitation") or not entry.get("impact"):
            _add(findings, "error", "trace", "Evidence ledger entry misses limitation or impact", {"entry": entry})


def _audit_loop_files(root: Path, findings: List[Finding]) -> None:
    for relative in REQUIRED_LOOP_FILES:
        path = root / relative
        if not path.exists():
            _add(findings, "error", "loop", "Required loop file is missing", {"file": relative})

    state_text = _read_text(root / "STATE.md")
    for pattern in STALE_STATE_PATTERNS:
        if pattern in state_text:
            _add(findings, "error", "loop_state", "STATE.md contains stale previous-loop action", {"pattern": pattern})

    budget_text = _read_text(root / "loop-budget.md")
    if "Autonomous Goal Loop" not in budget_text:
        _add(findings, "error", "loop_budget", "loop-budget.md does not distinguish Autonomous Goal Loop from Daily Triage")
    if "200k" not in budget_text:
        _add(findings, "warning", "loop_budget", "loop-budget.md should define an Autonomous Goal Loop checkpoint threshold")

    constraints_text = _read_text(root / "loop-constraints.md")
    if "Autonomous loop mode" not in constraints_text:
        _add(findings, "error", "loop_constraints", "loop-constraints.md misses autonomous loop push/update rule")
    if "degraded success" not in constraints_text.lower():
        _add(findings, "warning", "loop_constraints", "loop-constraints.md should explicitly treat fallback success as degraded success")
    if "AGENTS.md" not in constraints_text or "hard baseline" not in constraints_text:
        _add(findings, "error", "loop_constraints", "loop-constraints.md must keep AGENTS.md as the hard baseline")

    loop_text = _read_text(root / "LOOP.md")
    if "Autonomous Goal Loop Contract" not in loop_text:
        _add(findings, "error", "loop", "LOOP.md misses Autonomous Goal Loop Contract")
    if "AGENTS.md" not in loop_text or "hard baseline" not in loop_text:
        _add(findings, "error", "loop", "LOOP.md must keep AGENTS.md as the hard baseline")


def _audit_agent_operating_docs(root: Path, findings: List[Finding]) -> None:
    readme_text = _read_text(root / "README.md")
    missing_readme_markers = [
        marker for marker in REQUIRED_README_MARKERS
        if marker not in readme_text
    ]
    if missing_readme_markers:
        _add(
            findings,
            "error",
            "agent_docs",
            "README.md misses Agent operating model markers",
            {"missing": missing_readme_markers},
        )

    doc_path = root / AGENT_OPERATING_DOC
    doc_text = _read_text(doc_path)
    if not doc_text:
        _add(
            findings,
            "error",
            "agent_docs",
            "Agent loop/workflow glossary is missing",
            {"file": str(AGENT_OPERATING_DOC)},
        )
        return
    missing_doc_markers = [
        marker for marker in REQUIRED_AGENT_OPERATING_DOC_MARKERS
        if marker not in doc_text
    ]
    if missing_doc_markers:
        _add(
            findings,
            "error",
            "agent_docs",
            "Agent loop/workflow glossary misses required concepts or names",
            {"file": str(AGENT_OPERATING_DOC), "missing": missing_doc_markers},
        )


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""


def _add(
    findings: List[Finding],
    severity: str,
    area: str,
    message: str,
    detail: Optional[Dict[str, Any]] = None,
) -> None:
    findings.append(Finding(severity=severity, area=area, message=message, detail=detail or {}))


class _EmptyRegistry:
    def list_tools(self) -> List[Any]:
        return []

    def __contains__(self, name: str) -> bool:
        return False


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="Print JSON output instead of a compact text summary.")
    parser.add_argument("--fail-on-warning", action="store_true", help="Exit non-zero when warnings are present.")
    args = parser.parse_args(argv)

    result = run_audit()
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        summary = result["summary"]
        print(
            "Agent infra audit: "
            f"{summary['error_count']} errors, "
            f"{summary['warning_count']} warnings, "
            f"{summary['tool_count']} tools, "
            f"{summary['planner_capability_count']} planner capabilities"
        )
        for finding in result["findings"]:
            print(f"- [{finding['severity']}] {finding['area']}: {finding['message']} {finding['detail']}")

    if result["summary"]["error_count"] > 0:
        return 1
    if args.fail_on_warning and result["summary"]["warning_count"] > 0:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
