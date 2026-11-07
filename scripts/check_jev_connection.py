"""One small real TypeSafe request; prints no credential and writes no signal."""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.services.jev_client import JevClient, JevClientError  # noqa: E402


def main() -> int:
    try:
        result = JevClient().evaluate(
            {"purpose": "connectivity check", "application": "StockAnalyser", "mode": "shadow"},
            {"mode": {"type": "choice", "instructions": "Which mode is explicitly stated in the supplied state?",
                      "criteria": {"shadow": "The mode is shadow.", "live": "The mode is live."}}},
        )
    except JevClientError as exc:
        print(json.dumps({"status": "error", "reason": str(exc)}))
        return 1
    print(json.dumps({"status": "ok", "model": result["model"], "latency_ms": result["latency_ms"],
                      "usage": result["usage"], "answer": result["answers"]["mode"]["choice"]}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
