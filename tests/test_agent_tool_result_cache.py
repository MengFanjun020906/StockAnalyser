import json
from unittest.mock import patch

from src.agent.tools.result_cache import read_tool_result_cache, write_tool_result_cache


def test_tool_result_cache_round_trip(tmp_path):
    with patch("src.agent.tools.result_cache._CACHE_ROOT", tmp_path):
        write_tool_result_cache("get_capital_flow", "600519", {"status": "ok", "value": 1})
        payload, age_seconds = read_tool_result_cache("get_capital_flow", "600519", max_age_seconds=60)

    assert payload == {"status": "ok", "value": 1}
    assert 0 <= age_seconds < 5


def test_tool_result_cache_rejects_expired_payload(tmp_path):
    path = tmp_path / "get_chip_distribution" / "600519.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"cached_at": 1, "payload": {"status": "ok"}}), encoding="utf-8")

    with patch("src.agent.tools.result_cache._CACHE_ROOT", tmp_path), patch(
        "src.agent.tools.result_cache.time.time",
        return_value=100,
    ):
        payload, age_seconds = read_tool_result_cache(
            "get_chip_distribution",
            "600519",
            max_age_seconds=10,
        )

    assert payload is None
    assert age_seconds == 99
