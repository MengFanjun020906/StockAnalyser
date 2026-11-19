# -*- coding: utf-8 -*-
"""Factor registry for the unified hit-evaluation protocol.

The YAML file is the single source of truth for factor versions and evaluation
thresholds.  Nothing here reads environment variables: factor definitions must
not drift between machines.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Mapping, Optional

import yaml

DEFAULT_CONFIG_PATH = Path(__file__).with_name("factors.yaml")
_ALLOWED_DIRECTIONS = {"positive", "negative"}


class FactorConfigError(ValueError):
    """Raised when the factor registry is invalid."""


@dataclass(frozen=True)
class EvalDefaults:
    horizon: int
    cost_bps: int
    benchmark: str
    benchmark_name: str
    min_decision_dates: int
    promotion_decision_dates: int
    min_observations: int
    min_stocks_per_date: int
    min_coverage_not_evaluable: float
    min_coverage_pass: float
    min_executability: float
    min_mean_rank_ic: float
    min_icir: float
    min_ic_t: float
    min_positive_ic_ratio: float
    min_monotonicity: float
    min_long_short_ci_positive: bool
    min_partial_ic: float
    min_partial_t: float
    min_month_positive_ratio: float
    max_mde: float
    fdr: float
    bootstrap_rounds: int

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "EvalDefaults":
        if not isinstance(raw, Mapping):
            raise FactorConfigError("defaults must be a mapping")
        return cls(
            horizon=int(raw["horizon"]),
            cost_bps=int(raw["cost_bps"]),
            benchmark=str(raw["benchmark"]),
            benchmark_name=str(raw.get("benchmark_name") or raw["benchmark"]),
            min_decision_dates=int(raw["min_decision_dates"]),
            promotion_decision_dates=int(raw["promotion_decision_dates"]),
            min_observations=int(raw["min_observations"]),
            min_stocks_per_date=int(raw["min_stocks_per_date"]),
            min_coverage_not_evaluable=float(raw["min_coverage_not_evaluable"]),
            min_coverage_pass=float(raw["min_coverage_pass"]),
            min_executability=float(raw["min_executability"]),
            min_mean_rank_ic=float(raw["min_mean_rank_ic"]),
            min_icir=float(raw["min_icir"]),
            min_ic_t=float(raw["min_ic_t"]),
            min_positive_ic_ratio=float(raw["min_positive_ic_ratio"]),
            min_monotonicity=float(raw["min_monotonicity"]),
            min_long_short_ci_positive=bool(raw["min_long_short_ci_positive"]),
            min_partial_ic=float(raw["min_partial_ic"]),
            min_partial_t=float(raw["min_partial_t"]),
            min_month_positive_ratio=float(raw["min_month_positive_ratio"]),
            max_mde=float(raw["max_mde"]),
            fdr=float(raw["fdr"]),
            bootstrap_rounds=int(raw["bootstrap_rounds"]),
        )


@dataclass(frozen=True)
class FactorSpec:
    factor_id: str
    kind: str
    version: str
    description: str
    direction: str
    horizon: int
    benchmark: str
    lookback: int
    definition_hash: str

    @property
    def sign(self) -> float:
        return 1.0 if self.direction == "positive" else -1.0


@dataclass(frozen=True)
class FactorRegistry:
    defaults: EvalDefaults
    factors: Dict[str, FactorSpec]
    config_path: Path


# Bookkeeping fields record evaluation status, not the factor definition.  They
# must not change ``definition_hash`` or old reports stop matching the config.
_HASH_EXCLUDED_KEYS = {"status", "note", "notes"}


def _definition_hash(definition: Mapping[str, Any]) -> str:
    payload = json.dumps(
        {
            key: definition.get(key)
            for key in sorted(definition)
            if key not in _HASH_EXCLUDED_KEYS
        },
        ensure_ascii=False,
        sort_keys=True,
        default=str,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def load_registry(path: Optional[Path] = None) -> FactorRegistry:
    """Load and validate the factor registry from YAML."""

    config_path = Path(path) if path else DEFAULT_CONFIG_PATH
    if not config_path.exists():
        raise FactorConfigError(f"factor config not found: {config_path}")
    raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    defaults = EvalDefaults.from_mapping(raw.get("defaults") or {})
    if not defaults.benchmark:
        raise FactorConfigError("defaults.benchmark must not be empty")

    factors: Dict[str, FactorSpec] = {}
    for item in raw.get("factors") or []:
        if not isinstance(item, Mapping):
            raise FactorConfigError("each factor entry must be a mapping")
        factor_id = str(item.get("factor_id") or "").strip()
        if not factor_id:
            raise FactorConfigError("factor_id must not be empty")
        if factor_id in factors:
            raise FactorConfigError(f"duplicate factor_id: {factor_id}")
        direction = str(item.get("direction") or "").strip().lower()
        if direction not in _ALLOWED_DIRECTIONS:
            raise FactorConfigError(
                f"factor {factor_id}: direction must be one of {sorted(_ALLOWED_DIRECTIONS)}"
            )
        horizon = int(item.get("horizon", defaults.horizon))
        lookback = int(item.get("lookback", 0))
        if horizon < 1:
            raise FactorConfigError(f"factor {factor_id}: horizon must be >= 1")
        if lookback < 0:
            raise FactorConfigError(f"factor {factor_id}: lookback must be >= 0")
        benchmark = str(item.get("benchmark") or defaults.benchmark)
        factors[factor_id] = FactorSpec(
            factor_id=factor_id,
            kind=str(item.get("kind") or factor_id),
            version=str(item.get("version") or "0.0.0"),
            description=str(item.get("description") or ""),
            direction=direction,
            horizon=horizon,
            benchmark=benchmark,
            lookback=lookback,
            definition_hash=str(item.get("definition_hash") or _definition_hash(item)),
        )

    if not factors:
        raise FactorConfigError("factor config contains no factors")
    return FactorRegistry(defaults=defaults, factors=factors, config_path=config_path)


def get_factor(registry: FactorRegistry, factor_id: str) -> FactorSpec:
    try:
        return registry.factors[factor_id]
    except KeyError as exc:
        raise FactorConfigError(
            f"unknown factor_id: {factor_id}; available: {sorted(registry.factors)}"
        ) from exc
