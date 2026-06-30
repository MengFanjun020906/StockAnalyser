#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Build offline Agent verdict insight markdown from review JSONL."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.services.agent_verdict_review_service import AgentVerdictReviewService


def main() -> int:
    parser = argparse.ArgumentParser(description="Build Agent verdict insight markdown from local review JSONL.")
    parser.add_argument("--input", type=Path, default=None, help="Input JSONL path. Defaults to data/agent_reviews/verdict_review.jsonl.")
    parser.add_argument("--output", type=Path, default=None, help="Output Markdown path. Defaults to data/agent_reviews/insights/agent_verdict_insights.md.")
    parser.add_argument("--min-samples", type=int, default=20, help="Minimum completed samples in a group before it becomes a stable insight.")
    parser.add_argument("--top-n", type=int, default=12, help="Maximum groups to render in each table.")
    args = parser.parse_args()

    result = AgentVerdictReviewService().build_insight_markdown(
        input_path=args.input,
        output_path=args.output,
        min_samples=args.min_samples,
        top_n=args.top_n,
    )
    print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
