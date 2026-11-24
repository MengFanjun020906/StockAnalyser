#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""手工补跑决策结果结算（MEM-01a）。

只读本地 ``stock_daily``，不联网；等价于后台任务里每小时执行一次的结算函数。
用于在数据补齐后立即结算待处理记录，或排查边界状态分布。

用法：
    python scripts/settle_decision_outcomes.py
    python scripts/settle_decision_outcomes.py --as-of 2026-11-23
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.config import setup_env  # noqa: E402

setup_env()

from src.services.decision_outcome_service import settle_due_records  # noqa: E402


def _parse_date(value: Optional[str]) -> Optional[date]:
    if not value:
        return None
    return datetime.strptime(value.strip(), "%Y-%m-%d").date()


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(description="Settle pending decision outcome records (local data only).")
    parser.add_argument(
        "--as-of",
        dest="as_of",
        default=None,
        help="Settlement reference date (YYYY-MM-DD). Defaults to today.",
    )
    args = parser.parse_args(argv)

    try:
        as_of = _parse_date(args.as_of)
    except ValueError:
        print(f"invalid --as-of value: {args.as_of!r}, expected YYYY-MM-DD", file=sys.stderr)
        return 2

    try:
        summary = settle_due_records(as_of=as_of)
    except Exception as exc:  # pragma: no cover - defensive CLI branch
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1

    payload = {
        "as_of": (as_of or date.today()).isoformat(),
        "checked": summary.get("checked", 0),
        "updated": summary.get("updated", 0),
        "pending": summary.get("pending", 0),
        "settled": summary.get("settled", 0),
        "skipped": summary.get("skipped", 0),
        "expired": summary.get("expired", 0),
        "delisted": summary.get("delisted", 0),
    }
    if summary.get("error"):
        payload["error"] = summary["error"]
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
