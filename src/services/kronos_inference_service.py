# -*- coding: utf-8 -*-
"""Lazy Kronos model inference wrapper."""

from __future__ import annotations

import importlib
import logging
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Optional

import pandas as pd

from src.config import Config, get_config
from src.services.kronos_data_adapter import KronosPreparedData

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class KronosInferenceResult:
    """Normalized result from a Kronos forecast call."""

    status: str
    forecast_df: Optional[pd.DataFrame] = None
    model_profile: Dict[str, Any] = field(default_factory=dict)
    diagnostics: Dict[str, Any] = field(default_factory=dict)
    error: Optional[str] = None


class KronosInferenceService:
    """Load Kronos lazily and run daily forecasts."""

    def __init__(self, config: Optional[Config] = None, predictor: Any = None) -> None:
        self.config = config or get_config()
        self._predictor = predictor

    def is_enabled(self) -> bool:
        return bool(self._predictor is not None or self.config.kronos_enabled)

    def status(self) -> Dict[str, Any]:
        repo_path = str(self._resolve_repo_path())
        return {
            "enabled": self.is_enabled(),
            "repo_path": repo_path,
            "repo_available": Path(repo_path).exists(),
            "tokenizer_name": self.config.kronos_tokenizer_path,
            "model_name": self.config.kronos_model_path,
            "device": self.config.kronos_device,
            "max_context": self.config.kronos_max_context,
            "cache_enabled": self.config.kronos_cache_signals,
        }

    def predict_daily(self, prepared: KronosPreparedData, *, pred_len: int) -> KronosInferenceResult:
        if not self.is_enabled():
            return KronosInferenceResult(
                status="unavailable",
                error="Kronos model is disabled. Set KRONOS_ENABLED=true after weights are configured.",
                model_profile=self._model_profile(),
                diagnostics={"reason": "disabled_by_config"},
            )

        try:
            predictor = self._predictor or self._load_predictor()
            forecast_df = predictor.predict(
                df=prepared.bars_df[["open", "high", "low", "close", "volume", "amount"]],
                x_timestamp=prepared.x_timestamp,
                y_timestamp=prepared.y_timestamp,
                pred_len=int(pred_len),
                T=float(self.config.kronos_temperature),
                top_p=float(self.config.kronos_top_p),
                sample_count=int(self.config.kronos_sample_count),
                verbose=False,
            )
            return KronosInferenceResult(
                status="ok",
                forecast_df=forecast_df,
                model_profile=self._model_profile(),
                diagnostics={"pred_len": int(pred_len)},
            )
        except Exception as exc:  # pragma: no cover - exercised in integration environments
            logger.warning("Kronos inference failed for %s: %s", prepared.code, exc, exc_info=True)
            return KronosInferenceResult(
                status="error",
                error=str(exc),
                model_profile=self._model_profile(),
                diagnostics={"exception_type": type(exc).__name__},
            )

    def _load_predictor(self) -> Any:
        if self._predictor is not None:
            return self._predictor

        repo_path = self._resolve_repo_path()
        if not repo_path.exists():
            raise FileNotFoundError(f"Kronos repository not found: {repo_path}")

        repo_text = str(repo_path)
        if repo_text not in sys.path:
            sys.path.insert(0, repo_text)
        model_module = importlib.import_module("model")
        tokenizer = model_module.KronosTokenizer.from_pretrained(self.config.kronos_tokenizer_path)
        model = model_module.Kronos.from_pretrained(self.config.kronos_model_path)
        device = None if str(self.config.kronos_device or "").strip().lower() in {"", "auto"} else self.config.kronos_device
        self._predictor = model_module.KronosPredictor(
            model,
            tokenizer,
            device=device,
            max_context=int(self.config.kronos_max_context),
        )
        return self._predictor

    def _resolve_repo_path(self) -> Path:
        configured = Path(str(self.config.kronos_repo_path or "Kronos")).expanduser()
        if configured.is_absolute():
            return configured
        return Path(__file__).resolve().parents[2] / configured

    def _model_profile(self) -> Dict[str, Any]:
        return {
            "tokenizer_name": self.config.kronos_tokenizer_path,
            "model_name": self.config.kronos_model_path,
            "device": self.config.kronos_device,
            "max_context": self.config.kronos_max_context,
            "sample_count": self.config.kronos_sample_count,
            "top_p": self.config.kronos_top_p,
            "temperature": self.config.kronos_temperature,
        }
