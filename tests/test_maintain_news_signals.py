"""Tests for the bounded news-signal maintenance command."""

from scripts.maintain_news_signals import _env_choice, _incomplete_phases


def test_incomplete_phases_retries_partial_backfill_and_disabled_graph() -> None:
    result = {
        "event_backfill": {"status": "partial"},
        "graph_repair": {"status": "disabled"},
    }

    assert _incomplete_phases(result) == [
        {"phase": "event_backfill", "status": "partial"},
        {"phase": "graph_repair", "status": "disabled"},
    ]


def test_incomplete_phases_accepts_success_and_skipped_operations() -> None:
    result = {
        "event_backfill": {"status": "ok"},
        "graph_repair": {"status": "ok"},
        "outcomes": {"outcomes_upserted": 0},
    }

    assert _incomplete_phases(result) == []


def test_env_choice_falls_back_for_invalid_value(monkeypatch) -> None:
    monkeypatch.setenv("NEWS_SIGNAL_GRAPH_REPAIR_MODE", "invalid")

    assert _env_choice(
        "NEWS_SIGNAL_GRAPH_REPAIR_MODE",
        "edges",
        ("off", "edges", "episodes"),
    ) == "edges"
