# -*- coding: utf-8 -*-
"""Per-dimension tool manifest loader.

Loads `<dimension>.yaml` next to this module and returns a validated
`DimensionManifest`. The loader also performs consistency checks against:

1. The expert's tool whitelist (e.g. CAPITAL_FLOW_TOOLS) — every manifest tool
   must be in the whitelist; missing-from-manifest tools trigger a warning.
2. The global `ToolRegistry` — every manifest tool must exist there.
3. Each entry's `typical_args` keys must be a subset of the ToolDefinition's
   parameter names (catches stale arg names after a tool's signature changes).

Runtime placeholder substitution for `typical_args` (e.g. `{today}`,
`{seed_codes}`) happens in `render_typical_args()` at prompt build time, NOT at
load time, so the same loaded manifest can be reused across runs.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Sequence

import yaml

from src.agent.candidate_experts_v2.tools_manifest._schema import (
    DimensionManifest,
    ManifestEntry,
)

logger = logging.getLogger(__name__)

_MANIFEST_DIR = Path(__file__).resolve().parent


class ManifestValidationError(ValueError):
    """Raised when manifest content disagrees with whitelist or ToolRegistry."""


def _load_yaml(path: Path) -> Dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(f"manifest file not found: {path}")
    with path.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    if not isinstance(data, dict):
        raise ManifestValidationError(
            f"manifest {path} root must be a mapping, got {type(data).__name__}"
        )
    return data


def load_manifest(dimension: str) -> DimensionManifest:
    """Load and pydantic-validate `<dimension>.yaml`.

    Does NOT cross-check against whitelist / ToolRegistry. Use
    `validate_manifest()` for that step (it requires an expert context).
    """
    path = _MANIFEST_DIR / f"{dimension}.yaml"
    raw = _load_yaml(path)
    manifest = DimensionManifest.model_validate(raw)
    if manifest.dimension != dimension:
        raise ManifestValidationError(
            f"manifest {path} declares dimension='{manifest.dimension}' "
            f"but was loaded as '{dimension}'"
        )
    return manifest


def validate_manifest(
    manifest: DimensionManifest,
    *,
    whitelist: Sequence[str],
    tool_registry: Mapping[str, Any],
) -> None:
    """Cross-check manifest entries against the expert whitelist and registry.

    Raises ManifestValidationError on:
        - manifest tool not in whitelist
        - manifest tool missing from global ToolRegistry
        - typical_args key not a valid parameter name for the tool
    Logs a warning for whitelist tools that have no manifest entry.
    """
    whitelist_set = set(whitelist)
    manifest_tools = {entry.tool for entry in manifest.tools}

    extra = manifest_tools - whitelist_set
    if extra:
        raise ManifestValidationError(
            f"manifest contains tools not in whitelist: {sorted(extra)}"
        )

    missing_from_manifest = whitelist_set - manifest_tools
    if missing_from_manifest:
        logger.warning(
            "manifest[%s] missing entries for whitelist tools: %s",
            manifest.dimension,
            sorted(missing_from_manifest),
        )

    for entry in manifest.tools:
        tool_def = _registry_get(tool_registry, entry.tool)
        if tool_def is None:
            raise ManifestValidationError(
                f"manifest tool '{entry.tool}' is not registered in ToolRegistry"
            )
        # Only enforce typical_args ⊆ parameters when the registered value
        # actually carries ToolDefinition.parameters metadata. Test fixtures
        # frequently register plain callables under a name; in that case we
        # can't introspect parameter names and silently skip this check.
        if not hasattr(tool_def, "parameters"):
            continue
        valid_param_names = _tool_param_names(tool_def)
        if not valid_param_names:
            continue
        unknown = set(entry.typical_args.keys()) - valid_param_names
        if unknown:
            raise ManifestValidationError(
                f"manifest tool '{entry.tool}' has typical_args keys not in its "
                f"ToolDefinition parameters: {sorted(unknown)} "
                f"(valid: {sorted(valid_param_names)})"
            )


def _registry_get(tool_registry: Mapping[str, Any], name: str) -> Any:
    """Look up a ToolDefinition from either a `ToolRegistry` instance or a dict.

    BaseExpert accepts a plain dict for `tool_registry` in some paths; supporting
    both keeps validation usable in tests without constructing a real registry.
    """
    if hasattr(tool_registry, "get"):
        try:
            return tool_registry.get(name)
        except TypeError:
            pass
    if isinstance(tool_registry, Mapping):
        return tool_registry.get(name)
    return None


def _tool_param_names(tool_def: Any) -> set[str]:
    params = getattr(tool_def, "parameters", None) or []
    names: set[str] = set()
    for p in params:
        name = getattr(p, "name", None)
        if name:
            names.add(name)
    return names


def render_typical_args(
    entry: ManifestEntry,
    variables: Mapping[str, Any],
) -> Dict[str, Any]:
    """Substitute `{var}` placeholders in `typical_args` values.

    Only string-valued args are templated; other types pass through unchanged.
    Unknown placeholders are left as-is so the model can still see them.
    """
    rendered: Dict[str, Any] = {}
    for key, value in entry.typical_args.items():
        if isinstance(value, str):
            try:
                rendered[key] = value.format_map(_SafeDict(variables))
            except Exception:  # pragma: no cover - format_map with SafeDict is safe
                rendered[key] = value
        else:
            rendered[key] = value
    return rendered


class _SafeDict(dict):
    """dict subclass that returns `{key}` literal for missing keys in str.format_map."""

    def __init__(self, source: Mapping[str, Any]) -> None:
        super().__init__(source)

    def __missing__(self, key: str) -> str:
        return "{" + key + "}"


__all__ = [
    "DimensionManifest",
    "ManifestEntry",
    "ManifestValidationError",
    "load_manifest",
    "validate_manifest",
    "render_typical_args",
]
