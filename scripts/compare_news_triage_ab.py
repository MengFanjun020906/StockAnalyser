#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Deterministic A/B check for news-triage news input.

Two parts:

1. ``--live`` captures one real search snapshot, then replays the same items
   with triage ordering off/on and prints the order diff plus the resulting
   ``news_event`` evidence-card change (status / stance / missing_fields).
2. Historical replay scans ``data/agent_traces/*/selection_context.json`` and
   counts how many deep-dive results were blocked by ">=2 core dimensions
   missing" (rule 8 wording) before/after the ``report`` field is populated.

No LLM call unless ``--call-llm`` is passed.
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List, Sequence

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

RULE8_MISSING = {"missing", "tool_failed", "unknown"}
DEFAULT_SNAPSHOT = ROOT / "data" / "news_triage_ab_snapshot.json"
DEFAULT_OUT = ROOT / "data" / "news_triage_ab_report.json"


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stock-code", default="600519")
    parser.add_argument("--stock-name", default="贵州茅台")
    parser.add_argument("--live", action="store_true", help="capture a fresh search snapshot (network)")
    parser.add_argument("--snapshot", default=str(DEFAULT_SNAPSHOT))
    parser.add_argument("--traces", default=str(ROOT / "data" / "agent_traces"))
    parser.add_argument("--skip-replay", action="store_true")
    parser.add_argument("--call-llm", action="store_true", help="also compare the deep-dive LLM verdict (costly)")
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    return parser.parse_args()


def _load_dotenv() -> None:
    try:
        from dotenv import load_dotenv
    except Exception:
        return
    path = ROOT / ".env"
    if path.exists():
        load_dotenv(path)


def _capture_snapshot(code: str, name: str) -> Dict[str, Any]:
    from src.search_service import get_search_service

    service = get_search_service()
    stock_news = service.search_stock_news(code, name, max_results=8)
    intel_results = service.search_comprehensive_intel(
        stock_code=code,
        stock_name=name,
        max_searches=2,
    )
    news_items = [
        {
            "title": item.title,
            "snippet": item.snippet,
            "url": item.url,
            "source": item.source,
            "published_date": item.published_date,
        }
        for item in list(getattr(stock_news, "results", []) or [])
        if getattr(item, "title", "")
    ]
    intel_items: List[Dict[str, Any]] = []
    for dim_name, response in (intel_results or {}).items():
        if not (response and getattr(response, "success", False)):
            continue
        for item in list(response.results or [])[:4]:
            intel_items.append(
                {
                    "dim": dim_name,
                    "title": item.title,
                    "date": item.published_date or "",
                    "source": item.source,
                    "snippet": item.snippet or "",
                }
            )
    return {
        "stock_code": code,
        "stock_name": name,
        "stock_news": news_items,
        "intel": intel_items,
        "dimensions_searched": [
            dim for dim, resp in (intel_results or {}).items() if resp and resp.success
        ],
        "dimensions_empty": [
            dim for dim, resp in (intel_results or {}).items() if not (resp and resp.success)
        ],
    }


def _read_snapshot(args: argparse.Namespace) -> Dict[str, Any]:
    path = Path(args.snapshot)
    snapshot: Dict[str, Any] = {}
    if path.exists() and not args.live:
        snapshot = json.loads(path.read_text(encoding="utf-8"))
    elif args.live:
        try:
            snapshot = _capture_snapshot(args.stock_code, args.stock_name)
        except Exception as exc:
            print(f"[warn] live snapshot failed: {exc}")
            snapshot = {}
    else:
        snapshot = {}
    if not snapshot.get("stock_news") and not snapshot.get("intel"):
        # 网络不可用时回放历史 trace 里的真实 intel 条目，保证顺序对照可复现。
        snapshot = _snapshot_from_traces(args.traces)
    if snapshot:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")
    return snapshot or {"stock_code": args.stock_code, "stock_name": args.stock_name, "stock_news": [], "intel": []}


def _snapshot_from_traces(trace_root: str) -> Dict[str, Any]:
    from src.services.news_triage_input import annotate_and_sort_items

    paths = sorted(glob.glob(os.path.join(trace_root, "*", "selection_context.json")), reverse=True)
    best: Dict[str, Any] = {}
    best_rank = (-1, -1)
    for path in paths:
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        stages = data.get("stages") or {}
        candidates = ((stages.get("balanced_candidate_evidence") or {}).get("full") or {}).get("candidates") or []
        for candidate in candidates:
            evidence = candidate.get("raw_evidence") if isinstance(candidate.get("raw_evidence"), dict) else {}
            raw = evidence.get("search_comprehensive_intel") if isinstance(evidence, dict) else None
            items = (((raw or {}).get("intel") or {}).get("items") or []) if isinstance(raw, dict) else []
            if not items:
                continue
            normalized = [
                {
                    "dim": str(item.get("dim") or ""),
                    "title": str(item.get("title") or ""),
                    "date": str(item.get("date") or ""),
                    "source": str(item.get("source") or ""),
                    "snippet": str(item.get("snippet") or ""),
                }
                for item in items
                if isinstance(item, dict) and item.get("title")
            ]
            if not normalized:
                continue
            annotated = annotate_and_sort_items(normalized, enabled=True)
            scores = [int((item.get("news_triage") or {}).get("score") or 0) for item in annotated]
            spread = (max(scores) - min(scores)) if scores else 0
            reordered = bool(annotated) and annotated[0].get("title") != normalized[0].get("title")
            rank = (1 if reordered else 0, spread)
            if rank <= best_rank:
                continue
            best_rank = rank
            stock_news = _stock_news_from_raw(evidence.get("search_stock_news"))
            best = {
                "stock_code": str(candidate.get("code") or ""),
                "stock_name": str(candidate.get("name") or ""),
                "stock_news": stock_news,
                "intel": normalized,
                "dimensions_searched": sorted({item["dim"] for item in normalized if item["dim"]}),
                "dimensions_empty": [],
                "source": f"trace:{Path(path).parent.name}",
            }
    return best


def _stock_news_from_raw(raw: Any) -> List[Dict[str, Any]]:
    if not isinstance(raw, dict):
        return []
    results = raw.get("results")
    if not isinstance(results, list):
        return []
    items: List[Dict[str, Any]] = []
    for item in results:
        if not isinstance(item, dict) or not item.get("title"):
            continue
        items.append(
            {
                "title": str(item.get("title") or ""),
                "snippet": str(item.get("snippet") or ""),
                "source": str(item.get("source") or ""),
                "published_date": str(item.get("published_date") or ""),
            }
        )
    return items


def _order_table(items: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    rows = []
    for index, item in enumerate(items):
        triage = item.get("news_triage") or {}
        rows.append(
            {
                "index": index,
                "title": str(item.get("title") or "")[:46],
                "channel": triage.get("channel"),
                "category": triage.get("category"),
                "score": triage.get("score"),
                "priority": triage.get("priority"),
                "keep": triage.get("keep"),
            }
        )
    return rows


def _news_card(raw: Dict[str, Any], code: str, name: str):
    from src.agent.evidence.adapter import build_evidence_card
    from src.agent.evidence.schemas import StockRef

    return build_evidence_card(
        run_id="news-triage-ab",
        stock=StockRef(code=code, name=name, market="cn"),
        dimension="news_event",
        tool_name="search_comprehensive_intel",
        raw=raw,
        raw_ref="ab_snapshot",
    )


def _card_view(card) -> Dict[str, Any]:
    return {
        "status": card.data_quality.status,
        "missing_fields": list(card.data_quality.missing_fields or []),
        "stance": card.impact.stance,
        "action_bias": card.impact.action_bias,
        "score_delta": card.impact.score_delta,
        "signal_interpretations": [signal.interpretation for signal in card.signals],
    }


def _build_intel_raw(snapshot: Dict[str, Any], *, with_report: bool, brief: str) -> Dict[str, Any]:
    from src.services.news_triage_input import (
        annotate_and_sort_items,
        build_news_report,
        build_triage_summary,
    )

    items = annotate_and_sort_items(snapshot.get("intel") or [], enabled=True)
    raw: Dict[str, Any] = {
        "status": "ok" if items else "empty",
        "stock_code": snapshot["stock_code"],
        "stock_name": snapshot["stock_name"],
        "intel": {"items": items, "key_signals": [], "overall_sentiment": "unknown"},
        "dimensions_searched": snapshot.get("dimensions_searched") or [],
        "dimensions_empty": snapshot.get("dimensions_empty") or [],
        "source_chain": [{"provider": "search_comprehensive_intel", "result": "ok"}],
    }
    if with_report and items:
        raw["report"] = build_news_report(items)
        raw["summary"] = build_triage_summary(items)
        raw["news_triage_sorted"] = True
    if brief:
        raw["market_env_brief"] = brief
    return raw


def _compare_live(args: argparse.Namespace, report: Dict[str, Any]) -> None:
    from src.services.news_triage_input import (
        annotate_and_sort_items,
        build_market_env_brief,
    )

    snapshot = _read_snapshot(args)
    code = str(snapshot["stock_code"])
    name = str(snapshot["stock_name"])
    raw_news = list(snapshot.get("stock_news") or [])
    sorted_news = annotate_and_sort_items(raw_news, enabled=True)
    brief = build_market_env_brief()

    off_raw = _build_intel_raw(snapshot, with_report=False, brief="")
    on_raw = _build_intel_raw(snapshot, with_report=True, brief=brief)
    card_off = _news_card(off_raw, code, name)
    card_on = _news_card(on_raw, code, name)

    report["live"] = {
        "snapshot_source": snapshot.get("source") or "live",
        "stock": {"code": code, "name": name},
        "stock_news_order": {
            "off": _order_table(annotate_and_sort_items(raw_news, enabled=False)),
            "on": _order_table(sorted_news),
        },
        "intel_order": {
            "off": _order_table(annotate_and_sort_items(snapshot.get("intel") or [], enabled=False)),
            "on": _order_table(annotate_and_sort_items(snapshot.get("intel") or [], enabled=True)),
        },
        "news_report_preview": (on_raw.get("report") or "")[:600],
        "market_env_brief": brief,
        "news_event_card": {"off": _card_view(card_off), "on": _card_view(card_on)},
    }

    if args.call_llm:
        report["live"]["llm"] = _call_llm_compare(code, name, card_off, card_on, brief)


def _call_llm_compare(code: str, name: str, card_off, card_on, brief: str) -> Dict[str, Any]:
    from src.agent.stock_selection_prompts import build_deep_dive_prompt

    try:
        from src.agent.llm_adapter import LLMToolAdapter

        adapter = LLMToolAdapter()
    except Exception as exc:  # pragma: no cover - runtime packaging
        return {"error": f"LLM adapter unavailable: {exc}"}

    results: Dict[str, Any] = {}
    for label, card in (("off", card_off), ("on", card_on)):
        payload = {
            "user_message": f"分析 {name}（{code}）的消息面维度。",
            "stock_code": code,
            "stock_name": name,
            "stock_evidence": {"news_event": card.model_dump()},
        }
        if label == "on" and brief:
            payload["market_env_brief"] = brief
        prompt = build_deep_dive_prompt(payload)
        try:
            response = adapter.call_with_tools(
                [{"role": "user", "content": prompt}],
                [],
                timeout=120,
                response_format={"type": "json_object"},
            )
            parsed = _try_json(getattr(response, "content", "") or "")
        except Exception as exc:  # pragma: no cover - network/provider
            results[label] = {"error": str(exc)}
            continue
        summary = (parsed or {}).get("dimension_summary") if isinstance(parsed, dict) else None
        results[label] = {
            "news_event": (summary or {}).get("news_event") if isinstance(summary, dict) else None,
        }
    return results


def _try_json(text: str) -> Any:
    text = (text or "").strip()
    if text.startswith("```"):
        parts = text.split("```")
        text = parts[1].lstrip("json").strip() if len(parts) > 1 else text
    try:
        return json.loads(text)
    except ValueError:
        return None


def _replay_historical(trace_root: str) -> Dict[str, Any]:
    paths = sorted(glob.glob(os.path.join(trace_root, "*", "selection_context.json")))
    news_verdicts = Counter()
    before_blocked = 0
    after_blocked = 0
    released = 0
    news_missing_total = 0
    report_caused_total = 0
    deep_dives = 0
    released_wait = 0
    per_trace: List[Dict[str, Any]] = []
    candidate_blocked_before = 0
    candidate_blocked_after = 0
    candidates_total = 0

    for path in paths:
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        stages = data.get("stages") or {}
        local_before = local_after = local_released = 0

        results = ((stages.get("single_stock_deep_dive") or {}).get("full") or {}).get("results") or []
        for result in results:
            full = result.get("full") if isinstance(result.get("full"), dict) else {}
            dimensions = full.get("dimension_summary") if isinstance(full.get("dimension_summary"), dict) else {}
            if not dimensions:
                continue
            deep_dives += 1
            states = {
                dim: str((value or {}).get("verdict") or "").strip().lower()
                for dim, value in dimensions.items()
                if isinstance(value, dict)
            }
            news_value = dimensions.get("news_event") if isinstance(dimensions.get("news_event"), dict) else {}
            news_state = str((news_value or {}).get("verdict") or "").strip().lower()
            news_summary = str((news_value or {}).get("summary") or "")
            news_verdicts[news_state or "(empty)"] += 1
            report_caused = news_state in RULE8_MISSING and (
                "缺少report" in news_summary or "无有效摘要" in news_summary
            )
            missing = {dim for dim, state in states.items() if state in RULE8_MISSING}
            news_missing = "news_event" in missing
            news_missing_total += int(news_missing)
            report_caused_total += int(report_caused)
            before = len(missing)
            after = before - (1 if report_caused else 0)
            before_blocked += int(before >= 2)
            after_blocked += int(after >= 2)
            local_before += int(before >= 2)
            local_after += int(after >= 2)
            if before >= 2 and after < 2:
                released += 1
                local_released += 1
                if str(full.get("action_bias") or "").strip().lower() == "wait":
                    released_wait += 1

        candidates = ((stages.get("balanced_candidate_evidence") or {}).get("full") or {}).get("candidates") or []
        for candidate in candidates:
            schema = candidate.get("schema") if isinstance(candidate.get("schema"), dict) else {}
            if not schema:
                continue
            candidates_total += 1
            missing_candidates = {
                dim
                for dim, value in schema.items()
                if str((value or {}).get("status") or "").strip().lower() in RULE8_MISSING
            }
            candidate_blocked_before += int(len(missing_candidates) >= 2)
            candidate_blocked_after += int(len(missing_candidates - {"news_event"}) >= 2)

        if local_before or local_after:
            per_trace.append(
                {
                    "trace": Path(path).parent.name,
                    "blocked_before": local_before,
                    "blocked_after": local_after,
                    "released": local_released,
                }
            )

    return {
        "traces": len(paths),
        "deep_dives": deep_dives,
        "news_event_missing_total": news_missing_total,
        "news_event_report_caused": report_caused_total,
        "news_event_verdicts": dict(news_verdicts.most_common()),
        "blocked_before": before_blocked,
        "blocked_after": after_blocked,
        "released": released,
        "released_that_were_wait": released_wait,
        "still_blocked": after_blocked,
        "newly_blocked": max(0, after_blocked - before_blocked),
        "candidates_total": candidates_total,
        "candidate_blocked_before": candidate_blocked_before,
        "candidate_blocked_after": candidate_blocked_after,
        "candidate_released": candidate_blocked_before - candidate_blocked_after,
        "per_trace": per_trace[:20],
    }


def main() -> int:
    args = _parse_args()
    _load_dotenv()
    report: Dict[str, Any] = {"config": {
        "NEWS_TRIAGE_SEARCH_SORT_ENABLED": os.getenv("NEWS_TRIAGE_SEARCH_SORT_ENABLED", ""),
        "NEWS_TRIAGE_MARKET_BRIEF_ENABLED": os.getenv("NEWS_TRIAGE_MARKET_BRIEF_ENABLED", ""),
    }}
    _compare_live(args, report)
    if not args.skip_replay:
        report["replay"] = _replay_historical(args.traces)

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    live = report["live"]
    print("== stock news order ==")
    for row in live["stock_news_order"]["on"][:10]:
        print(f"  {row['index']:>2}. score={row['score']} {row['channel']} | {row['title']}")
    card = live["news_event_card"]
    print("== news_event card ==")
    print("  off:", card["off"]["status"], card["off"]["stance"], card["off"]["missing_fields"])
    print("  on :", card["on"]["status"], card["on"]["stance"], card["on"]["missing_fields"])
    if report.get("replay"):
        replay = report["replay"]
        print("== historical replay ==")
        print(f"  deep_dives={replay['deep_dives']} news_event_missing={replay['news_event_missing_total']}"
              f" report_caused={replay['news_event_report_caused']}")
        print(f"  blocked>=2 off={replay['blocked_before']} on={replay['blocked_after']} released={replay['released']}")
        print(f"  candidate blocked off={replay['candidate_blocked_before']} on={replay['candidate_blocked_after']}")
    print("report:", out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
