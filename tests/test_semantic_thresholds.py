"""Tests for model-specific semantic edge threshold profiles."""

from src.services.graphiti.semantic_thresholds import resolve_semantic_threshold


def test_resolve_semantic_threshold_matches_model_suffix_and_reports_profile() -> None:
    profile = resolve_semantic_threshold(
        "ollama/mxbai-embed-large",
        profiles_json='{"default": 0.8, "mxbai-embed-large": 0.76}',
    )

    assert profile["threshold"] == 0.76
    assert profile["matched_key"] == "mxbai-embed-large"
    assert profile["profile"] == "embedding-threshold:mxbai-embed-large"


def test_resolve_semantic_threshold_uses_bounded_default_for_unknown_model() -> None:
    profile = resolve_semantic_threshold(
        "text-embedding-unknown",
        profiles_json='{"default": 1.5}',
    )

    assert profile["threshold"] == 0.95
    assert profile["matched_key"] == "default"
