# -*- coding: utf-8 -*-
"""Unified factor hit-evaluation protocol (OBS-03b).

Public entry point: :func:`evaluate_factor`.
"""

from .evaluator import evaluate_factor
from .config import FactorRegistry, FactorSpec, load_registry

__all__ = ["evaluate_factor", "FactorRegistry", "FactorSpec", "load_registry"]
