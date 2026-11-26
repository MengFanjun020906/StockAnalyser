# -*- coding: utf-8 -*-
"""One-off backfill for the news event-study foundation.

Fills ``stock_codes_json`` / ``stock_scope`` / ``trade_date`` /
``event_category`` on existing ``raw_news_episodes`` and
``news_extracted_events`` rows. It never re-fetches news and never invents a
security code: rows without a resolvable target are marked ``no_stock``.

Usage:
    python scripts/backfill_news_event_foundation.py --dry-run
    python scripts/backfill_news_event_foundation.py
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any, Dict

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sqlalchemy import select

from src.config import get_config
from src.data.stock_name_index import get_a_share_name_matcher
from src.services.news_event_foundation import (
    build_event_foundation_fields,
    build_raw_foundation_fields,
    load_trading_calendar,
    summarize_scope,
)
from src.services.news_no_stock_triage import triage_news_item
from src.storage import DatabaseManager, NewsExtractedEvent, RawNewsEpisode, StockDaily

DEFAULT_REPORT = ROOT / "data" / "news_event_foundation_backfill.json"
BATCH_SIZE = 500


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="compute coverage without writing")
    parser.add_argument("--limit", type=int, default=0, help="limit rows per table (0 = all)")
    parser.add_argument("--batch-size", type=int, default=BATCH_SIZE)
    parser.add_argument("--report-path", default=str(DEFAULT_REPORT))
    return parser.parse_args()


def _json_load(value: Any) -> Any:
    if isinstance(value, (list, dict)):
        return value
    if not value:
        return []
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return []


def _apply_raw(row: RawNewsEpisode, fields: Dict[str, Any], triage: Dict[str, Any] | None = None) -> None:
    row.stock_codes_json = json.dumps(fields["stock_codes"], ensure_ascii=False)
    row.stock_scope = fields["stock_scope"]
    row.trade_date = fields["trade_date"]
    row.event_category = fields["event_category"]
    row.event_category_rule = fields["event_category_rule"]
    row.event_categories_json = json.dumps(fields["event_categories"], ensure_ascii=False)
    row.no_stock_triage_json = json.dumps(triage, ensure_ascii=False) if triage else None


def _apply_event(row: NewsExtractedEvent, fields: Dict[str, Any]) -> None:
    row.stock_codes_json = json.dumps(fields["stock_codes"], ensure_ascii=False)
    row.stock_scope = fields["stock_scope"]
    row.primary_stock_code = fields["primary_stock_code"] or None
    row.trade_date = fields["trade_date"]
    row.event_category = fields["event_category"]
    row.event_category_rule = fields["event_category_rule"]


def _raw_payload(row: RawNewsEpisode) -> Dict[str, Any]:
    return {
        "title": row.title,
        "summary": row.summary,
        "content": row.content,
        "normalized_content": row.normalized_content,
        "subjects": _json_load(row.subjects_json),
        "stocks": _json_load(row.stocks_json),
        "published_at": row.published_at,
        "signal_date": row.signal_date,
    }


def _event_payload(row: NewsExtractedEvent) -> Dict[str, Any]:
    return {
        "evidence_sentence": row.evidence_sentence,
        "trigger": row.trigger,
        "subject": row.subject,
        "object": row.object,
        "entity_links": _json_load(row.entity_links_json),
        "event_type": row.event_type,
        "event_time": row.event_time,
        "signal_date": row.signal_date,
    }


def main() -> int:
    args = _parse_args()
    manager = DatabaseManager.get_instance()
    config = get_config()
    triage_enabled = bool(getattr(config, "news_no_stock_triage_enabled", False))
    triage_matcher = (
        get_a_share_name_matcher(getattr(config, "news_no_stock_triage_stock_basic_path", "") or None)
        if triage_enabled
        else None
    )
    report: Dict[str, Any] = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "dry_run": bool(args.dry_run),
        "rules": {
            "trade_date": "pre-close same session; post-close/non-trading rolls to next session",
            "stock_scope": "single / multi / no_stock (unknown only for legacy rows)",
            "event_category": "ordered keyword rules in src/services/news_event_foundation.py",
            "no_stock_triage": "display-only; persisted as no_stock_triage_json when NEWS_NO_STOCK_TRIAGE_ENABLED=true",
        },
        "no_stock_triage_enabled": triage_enabled,
    }

    with manager.get_session() as session:
        calendar = load_trading_calendar(
            session.execute(select(StockDaily.date).distinct()).scalars().all()
        )
        report["trading_calendar"] = {
            "sessions": len(calendar.dates),
            "start": calendar.dates[0].isoformat() if calendar.dates else None,
            "end": calendar.dates[-1].isoformat() if calendar.dates else None,
        }

        raw_rows = session.execute(select(RawNewsEpisode)).scalars().all()
        if args.limit:
            raw_rows = raw_rows[: args.limit]
        raw_scope = Counter()
        raw_categories = Counter()
        raw_trade_date = 0
        triage_categories = Counter()
        triage_channels = Counter()
        triage_scores = Counter()
        triage_keep = 0
        triage_drop = 0
        triage_priority_positive = 0
        raw_payloads: Dict[str, Dict[str, Any]] = {}
        for index, row in enumerate(raw_rows, start=1):
            payload = _raw_payload(row)
            fields = build_raw_foundation_fields(
                payload,
                calendar=calendar,
                name_matcher=triage_matcher.find if triage_matcher is not None else None,
            )
            raw_scope[fields["stock_scope"]] += 1
            raw_categories[fields["event_category"]] += 1
            if fields["trade_date"] is not None:
                raw_trade_date += 1
            triage = None
            if triage_enabled and fields["stock_scope"] == "no_stock":
                triage = triage_news_item(payload, name_matcher=triage_matcher).to_dict()
                triage_categories[triage["category"]] += 1
                triage_channels[triage["channel"] or "(none)"] += 1
                triage_scores[int(triage["score"])] += 1
                if triage["keep"]:
                    triage_keep += 1
                else:
                    triage_drop += 1
                if int(triage["priority"]) > 0:
                    triage_priority_positive += 1
            raw_payloads[str(row.episode_id)] = {
                "stock_codes": fields["stock_codes"],
                "trade_date": fields["trade_date"],
                "published_at": row.published_at,
                "signal_date": row.signal_date,
            }
            if not args.dry_run:
                _apply_raw(row, fields, triage)
                if index % args.batch_size == 0:
                    session.commit()
        if not args.dry_run:
            session.commit()

        event_rows = session.execute(select(NewsExtractedEvent)).scalars().all()
        if args.limit:
            event_rows = event_rows[: args.limit]
        event_scope = Counter()
        event_categories = Counter()
        event_rules = Counter()
        event_trade_date = 0
        event_primary = 0
        event_fallback_to_raw = 0
        for index, row in enumerate(event_rows, start=1):
            raw = raw_payloads.get(str(row.raw_episode_id)) or {}
            fields = build_event_foundation_fields(_event_payload(row), raw=raw, calendar=calendar)
            event_scope[fields["stock_scope"]] += 1
            event_categories[fields["event_category"]] += 1
            event_rules[fields["event_category_rule"]] += 1
            if fields["trade_date"] is not None:
                event_trade_date += 1
            if fields["primary_stock_code"]:
                event_primary += 1
            if fields["stock_codes"] and raw.get("stock_codes") and not _json_load(row.entity_links_json):
                event_fallback_to_raw += 1
            if not args.dry_run:
                _apply_event(row, fields)
                if index % args.batch_size == 0:
                    session.commit()
        if not args.dry_run:
            session.commit()

    raw_total = len(raw_rows)
    event_total = len(event_rows)
    report["raw_news_episodes"] = {
        "total": raw_total,
        "stock_scope": dict(raw_scope),
        "coverage": summarize_scope(raw_scope),
        "trade_date_resolved": raw_trade_date,
        "trade_date_coverage_pct": round(raw_trade_date / raw_total * 100, 2) if raw_total else 0.0,
        "event_category": dict(raw_categories.most_common()),
    }
    if triage_enabled:
        report["raw_news_episodes"]["no_stock_triage"] = {
            "total": sum(triage_categories.values()),
            "keep": triage_keep,
            "drop": triage_drop,
            "priority_positive": triage_priority_positive,
            "channel": dict(triage_channels.most_common()),
            "category": dict(triage_categories.most_common()),
            "score_distribution": dict(sorted(triage_scores.items(), reverse=True)),
        }
    report["news_extracted_events"] = {
        "total": event_total,
        "stock_scope": dict(event_scope),
        "coverage": summarize_scope(event_scope),
        "trade_date_resolved": event_trade_date,
        "trade_date_coverage_pct": round(event_trade_date / event_total * 100, 2) if event_total else 0.0,
        "primary_stock_code": event_primary,
        "codes_from_raw_fallback": event_fallback_to_raw,
        "event_category": dict(event_categories.most_common()),
        "event_category_rule": dict(event_rules.most_common()),
    }
    report_text = json.dumps(report, ensure_ascii=False, indent=2)
    print(report_text)
    if not args.dry_run:
        report_path = Path(args.report_path)
        if not report_path.is_absolute():
            report_path = ROOT / report_path
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(report_text + "\n", encoding="utf-8")
        print(f"\n[report] {report_path}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
