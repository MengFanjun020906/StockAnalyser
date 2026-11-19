# -*- coding: utf-8 -*-
"""类型化反证清单 (FAC-02).

与 :mod:`veto_gate` 的确定性红线互补: veto_gate 只回答"能不能留",本模块回答
"这只票有哪些可审计的反面事实"。两种机制都**只否决不额外加分**。

设计约束:

- **默认只记录**: ``attach_counter_evidence(..., veto_enabled=False)`` 只把反证
  写进候选 payload 的 ``counter_evidence`` / ``risks``,不改变候选顺序、置信度、
  分数或撮合结果。
- **否决在开关后**: 仅当 ``veto_enabled=True`` 时,``veto_eligible`` 的候选才会
  被移出候选列表;开关默认关闭(``AGENT_COUNTER_EVIDENCE_VETO_ENABLED=false``)。
- **可审计**: 每条反证带 ``type`` / ``severity`` / ``reason`` / ``source`` /
  ``as_of`` / ``metrics``;数据不足时写入 ``unknown_checks``,不伪造"未命中"。
- **不重复造数据**: 只读 FactSheet + recall 层 FeatureFlag.metrics,不新增网络
  请求,不新增第二套数据源。

类型与判定口径见 ``COUNTER_EVIDENCE_TYPES`` 及 ``docs/architecture/选股链路重构-实施方案.md``。
"""

from __future__ import annotations

import math
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

COUNTER_EVIDENCE_TYPES: Tuple[str, ...] = (
    "theme_mismatch",
    "missing_market_data",
    "overextended_price",
    "weak_technical_confirmation",
    "negative_event_dominant",
    "extreme_valuation",
)

# 只有这两类"事实性缺失/错配"允许在开关开启后否决;其余类型只记录。
VETO_TYPES: Tuple[str, ...] = ("theme_mismatch", "missing_market_data")
VETO_TYPE_SEVERITY = 0.7
ANY_TYPE_VETO_SEVERITY = 0.9

_NEWS_KINDS = {"news"}


def _get(obj: Any, key: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _num(value: Any) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = float(value)
        return parsed if math.isfinite(parsed) else None
    except Exception:
        return None


def _flag_dicts(row: Any) -> List[Dict[str, Any]]:
    flags = _get(row, "flags", None) or []
    out: List[Dict[str, Any]] = []
    for flag in flags:
        if isinstance(flag, dict):
            out.append(flag)
            continue
        if hasattr(flag, "model_dump"):
            try:
                out.append(flag.model_dump())
                continue
            except Exception:
                pass
        out.append(
            {
                "detector": str(_get(flag, "detector", "") or ""),
                "kind": str(_get(flag, "kind", "unknown") or "unknown"),
                "metrics": _get(flag, "metrics", {}) or {},
                "as_of": str(_get(flag, "as_of", "unknown") or "unknown"),
            }
        )
    return out


def _metric_index(flags: Sequence[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """Flatten FeatureFlag.metrics into {metric: {value, detector, as_of}}.

    Nested dict metric values (e.g. a signal value ``{"pe": 12, "pb": 1.4}``)
    are flattened one level so callers can look up ``pe`` / ``pb`` / ``stock_role``
    without knowing which detector produced them. First write wins, so the
    earliest (highest-priority) source keeps provenance.
    """
    index: Dict[str, Dict[str, Any]] = {}

    def put(key: str, value: Any, detector: str, as_of: str) -> None:
        if key in index or value is None:
            return
        index[key] = {"value": value, "detector": detector or "unknown", "as_of": as_of or "unknown"}

    for flag in flags:
        detector = str(flag.get("detector") or "") or "unknown"
        as_of = str(flag.get("as_of") or "") or "unknown"
        metrics = flag.get("metrics")
        if not isinstance(metrics, dict):
            continue
        for key, value in metrics.items():
            if isinstance(value, dict):
                for nested_key, nested_value in value.items():
                    put(str(nested_key), nested_value, detector, as_of)
            else:
                put(str(key), value, detector, as_of)
    return index


def _add_item(
    items: List[Dict[str, Any]],
    type_: str,
    severity: float,
    reason: str,
    *,
    source: str,
    as_of: str,
    metrics: Optional[Dict[str, Any]] = None,
) -> None:
    items.append(
        {
            "type": type_,
            "severity": round(max(0.0, min(1.0, severity)), 3),
            "reason": str(reason)[:240],
            "source": str(source or "unknown"),
            "as_of": str(as_of or "unknown"),
            "metrics": metrics or {},
        }
    )


def _add_unknown(
    unknowns: List[Dict[str, Any]],
    check: str,
    reason: str,
    *,
    source: str,
    as_of: str = "unknown",
    metrics: Optional[Dict[str, Any]] = None,
) -> None:
    unknowns.append(
        {
            "check": check,
            "status": "insufficient_data",
            "reason": str(reason)[:240],
            "source": str(source or "unknown"),
            "as_of": str(as_of or "unknown"),
            "metrics": metrics or {},
        }
    )


def _confirmation_labels(confirmation: Any) -> List[str]:
    if not isinstance(confirmation, dict):
        return []
    return [
        str(item.get("value"))
        for item in (confirmation.get("labels") or [])
        if isinstance(item, dict) and str(item.get("value") or "").strip()
    ]


def _theme_counter(
    index: Dict[str, Dict[str, Any]],
    items: List[Dict[str, Any]],
    unknowns: List[Dict[str, Any]],
    theme_profile: Any = None,
    fallback_as_of: str = "",
) -> None:
    """Emit ``theme_mismatch`` only for confirmed unrelated seeds.

    ``theme_status`` has three states:

    - ``matched``: an alias hit; no counter evidence.
    - ``unrelated_confirmed``: positive evidence of another theme / an
      exclusion rule; this is the *only* state eligible to become a veto item.
    - ``unclassified``: evidence insufficient. Recorded in ``unknown_checks``
      and never treated as "unrelated".
    """

    profile = theme_profile if isinstance(theme_profile, dict) else {}
    role_entry = index.get("stock_role")
    regime_entry = index.get("theme_regime")
    status_entry = index.get("theme_status")

    profile_role = str(profile.get("stock_role") or "").strip()
    profile_regime = str(profile.get("theme_regime") or "").strip()
    profile_status = str(profile.get("theme_status") or "").strip()
    if profile:
        entry = role_entry or regime_entry or status_entry or {}
        evidence = [str(item) for item in (profile.get("evidence") or []) if str(item).strip()]
        evidence_source = str(profile.get("evidence_source") or "seed.theme_profile")
        as_of = str(profile.get("as_of") or entry.get("as_of") or fallback_as_of or "unknown")
        attribution = profile.get("theme_attribution") if isinstance(profile.get("theme_attribution"), dict) else {}
        attribution_missing = str(attribution.get("missing_reason") or "").strip()
        attribution_sources = [
            {
                "source_type": item.get("source_type"),
                "source_id": item.get("source_id"),
                "as_of": item.get("as_of"),
            }
            for item in (attribution.get("sources") or [])[:5]
            if isinstance(item, dict)
        ]
        confirmation = profile.get("theme_confirmation") if isinstance(profile.get("theme_confirmation"), dict) else {}
        attribution_confirmation = (
            attribution.get("confirmation") if isinstance(attribution.get("confirmation"), dict) else {}
        )
        evidence_kind = str(
            confirmation.get("evidence_kind") or attribution_confirmation.get("evidence_kind") or ""
        )
        labels = _confirmation_labels(confirmation) or _confirmation_labels(attribution_confirmation)
        base_metrics = {
            "theme_status": profile_status or "missing",
            "stock_role": profile_role,
            "theme_regime": profile_regime,
            "theme_confidence": profile.get("theme_confidence"),
            "membership_confidence": profile.get("membership_confidence"),
            "evidence_kind": evidence_kind,
            "theme_labels": labels,
            "evidence": evidence[:3],
            "theme_attribution_decision": attribution.get("decision"),
            "theme_attribution_missing_reason": attribution_missing,
            "theme_attribution_sources": attribution_sources,
        }

        if profile_status == "unrelated_confirmed":
            severity = (
                _num(confirmation.get("severity"))
                or _num(attribution_confirmation.get("severity"))
                or 0.7
            )
            basis = attribution_missing or (evidence[0] if evidence else "theme_profile")
            _add_item(
                items,
                "theme_mismatch",
                severity,
                f"主题确认无关（{evidence_kind or 'unknown'}）: {basis}",
                source=evidence_source,
                as_of=as_of,
                metrics=base_metrics,
            )
            return
        if profile_status in {"unclassified", "unknown"}:
            _add_unknown(
                unknowns,
                "theme_mismatch",
                "主题归属证据不足（theme_status=unclassified）："
                + (attribution_missing or "未命中主题别名且无他主题/排除正证据")
                + "；只记录，不判为无关",
                source=evidence_source,
                as_of=as_of,
                metrics=base_metrics,
            )
            return
        if profile_status == "matched":
            return
        # 旧快照没有 theme_status:unrelated/unknown 只代表"没查到/低置信"，
        # 不能作为与主题无关的证据。
        if profile_role in {"", "unrelated", "unknown"} and profile_regime in {"", "unknown", "theme_risk_off"}:
            _add_unknown(
                unknowns,
                "theme_mismatch",
                "旧 theme_profile 缺少 theme_status：stock_role="
                f"{profile_role or 'missing'} 只代表未命中/低置信，不能作为与主题无关的证据；只记录",
                source=evidence_source,
                as_of=as_of,
                metrics=base_metrics,
            )
        return

    if role_entry is None and regime_entry is None and status_entry is None:
        _add_unknown(
            unknowns,
            "theme_mismatch",
            "缺少 theme_profile(stock_role/theme_regime),无法判断主题归属",
            source="theme_momentum",
        )
        return

    role = str((role_entry or {}).get("value") or "")
    regime = str((regime_entry or {}).get("value") or "")
    theme_status = str((status_entry or {}).get("value") or "").strip()
    entry = role_entry or regime_entry or status_entry or {}
    entry_source = str(entry.get("detector") or "theme_momentum")
    entry_as_of = str(entry.get("as_of") or "unknown")
    flat_metrics = {
        "theme_status": theme_status or "missing",
        "stock_role": role,
        "theme_regime": regime,
    }
    if theme_status == "unrelated_confirmed":
        _add_item(
            items,
            "theme_mismatch",
            0.7,
            f"主题确认无关（explicit_other_theme）: stock_role={role or 'unknown'}, theme_regime={regime or 'unknown'}",
            source=entry_source,
            as_of=entry_as_of,
            metrics=flat_metrics,
        )
    elif theme_status in {"unclassified", "unknown"} or role in {"", "unrelated", "unknown"}:
        _add_unknown(
            unknowns,
            "theme_mismatch",
            "主题归属证据不足:stock_role="
            f"{role or 'missing'}, theme_status={theme_status or 'missing'}；只记录，不判为无关",
            source=entry_source,
            as_of=entry_as_of,
            metrics=flat_metrics,
        )


def _missing_data_counter(
    fact_sheet: Any,
    items: List[Dict[str, Any]],
    unknowns: List[Dict[str, Any]],
) -> None:
    if fact_sheet is None:
        _add_item(
            items,
            "missing_market_data",
            0.7,
            "未拿到 FactSheet,无法做任何价格/技术核验",
            source="FactSheet",
            as_of="unknown",
        )
        return

    warnings = [str(item) for item in (_get(fact_sheet, "warnings", None) or [])]
    points = [
        _num(_get(fact_sheet, "range_pct_60")),
        _num(_get(fact_sheet, "rsi14")),
        _num(_get(fact_sheet, "volume_ratio")),
    ]
    freshness = str(_get(fact_sheet, "freshness", "unknown") or "unknown")
    if "insufficient_daily_bars" in warnings:
        _add_item(
            items,
            "missing_market_data",
            0.7,
            "本地日线不足(insufficient_daily_bars),技术事实不可信",
            source="fact_sheet:warnings",
            as_of=freshness,
            metrics={"warnings": warnings},
        )
    elif all(value is None for value in points):
        _add_item(
            items,
            "missing_market_data",
            0.6,
            "FactSheet 缺 range_pct_60 / rsi14 / volume_ratio,技术事实不完整",
            source="fact_sheet:position_rsi_volume",
            as_of=freshness,
            metrics={"warnings": warnings},
        )


def _overextended_counter(
    fact_sheet: Any,
    index: Dict[str, Dict[str, Any]],
    items: List[Dict[str, Any]],
    unknowns: List[Dict[str, Any]],
) -> None:
    if fact_sheet is None:
        return
    gain = _num(_get(fact_sheet, "gain_5d"))
    bias = _num(_get(fact_sheet, "bias_ma20"))
    range_pct = _num(_get(fact_sheet, "range_pct_60"))
    freshness = str(_get(fact_sheet, "freshness", "unknown") or "unknown")
    if gain is None and bias is None and range_pct is None:
        _add_unknown(
            unknowns,
            "overextended_price",
            "FactSheet 缺 gain_5d / bias_ma20 / range_pct_60",
            source="fact_sheet",
            as_of=freshness,
        )
        return

    severity = 0.0
    reasons: List[str] = []
    if gain is not None and gain >= 15.0:
        severity = max(severity, min(0.85, 0.5 + (gain - 15.0) / 60.0))
        reasons.append(f"近5日涨幅 {gain:.1f}% >= 15%")
    if bias is not None and bias >= 12.0:
        severity = max(severity, min(0.8, 0.45 + (bias - 12.0) / 40.0))
        reasons.append(f"MA20 乖离 {bias:.1f}% >= 12%")
    if range_pct is not None and range_pct >= 0.95 and gain is not None and gain >= 8.0:
        severity = max(severity, 0.6)
        reasons.append(f"60日区间分位 {range_pct:.2f} 且近5日涨幅 {gain:.1f}%")
    if severity <= 0.0:
        return
    _add_item(
        items,
        "overextended_price",
        severity,
        "; ".join(reasons),
        source="fact_sheet:gain_5d/bias_ma20/range_pct_60",
        as_of=freshness,
        metrics={"gain_5d": gain, "bias_ma20": bias, "range_pct_60": range_pct},
    )


def _weak_technical_counter(
    fact_sheet: Any,
    items: List[Dict[str, Any]],
    unknowns: List[Dict[str, Any]],
) -> None:
    if fact_sheet is None:
        return
    trend = str(_get(fact_sheet, "trend_state", "unknown") or "unknown")
    rsi = _num(_get(fact_sheet, "rsi14"))
    volume_ratio = _num(_get(fact_sheet, "volume_ratio"))
    bias = _num(_get(fact_sheet, "bias_ma20"))
    freshness = str(_get(fact_sheet, "freshness", "unknown") or "unknown")

    if trend == "unknown" and rsi is None:
        _add_unknown(
            unknowns,
            "weak_technical_confirmation",
            "趋势状态与 RSI 均缺失,无法判断技术确认强弱",
            source="fact_sheet",
            as_of=freshness,
        )
        return
    if trend == "bearish":
        _add_item(
            items,
            "weak_technical_confirmation",
            0.45,
            f"趋势状态 bearish,与看多论点相反(rsi14={rsi})",
            source="fact_sheet:trend_state",
            as_of=freshness,
            metrics={"trend_state": trend, "rsi14": rsi},
        )
        return
    if trend == "neutral" and rsi is not None and rsi < 45.0 and (volume_ratio is None or volume_ratio < 1.0):
        _add_item(
            items,
            "weak_technical_confirmation",
            0.3,
            f"neutral 趋势下 RSI={rsi:.1f}<45 且量比不足,技术确认偏弱",
            source="fact_sheet:trend_state/rsi14/volume_ratio",
            as_of=freshness,
            metrics={"trend_state": trend, "rsi14": rsi, "volume_ratio": volume_ratio},
        )
    elif trend == "neutral" and bias is not None and bias < -5.0 and (volume_ratio is None or volume_ratio < 1.0):
        _add_item(
            items,
            "weak_technical_confirmation",
            0.3,
            f"neutral 趋势下 MA20 乖离 {bias:.1f}% 且量比不足,技术确认偏弱",
            source="fact_sheet:trend_state/bias_ma20/volume_ratio",
            as_of=freshness,
            metrics={"trend_state": trend, "bias_ma20": bias, "volume_ratio": volume_ratio},
        )


def _negative_event_counter(
    flags: Sequence[Dict[str, Any]],
    index: Dict[str, Dict[str, Any]],
    items: List[Dict[str, Any]],
    unknowns: List[Dict[str, Any]],
) -> None:
    # ``news_theme_daily`` currently resolves to detector="news_theme_daily"
    # with kind="unknown" in recall.py, so match by detector prefix as well.
    news_flags = [
        flag
        for flag in flags
        if str(flag.get("kind") or "") in _NEWS_KINDS
        or str(flag.get("detector") or "").startswith("news")
    ]
    neg_entry = index.get("negative_score")
    pos_entry = index.get("positive_score")
    neutral_entry = index.get("neutral_score")
    polarity_entry = index.get("polarity")

    negative = _num((neg_entry or {}).get("value"))
    positive = _num((pos_entry or {}).get("value"))
    neutral = _num((neutral_entry or {}).get("value"))
    polarity = str((polarity_entry or {}).get("value") or "").lower()

    if negative is not None and negative >= 0.45 and negative >= max(positive or 0.0, neutral or 0.0):
        entry = neg_entry or {}
        sentiment_as_of = index.get("news_sentiment_as_of")
        evidence_entry = index.get("news_counter_evidence")
        evidence_cards = evidence_entry.get("value") if isinstance(evidence_entry, dict) else []
        _add_item(
            items,
            "negative_event_dominant",
            min(0.9, 0.35 + negative * 0.55),
            f"负向事件分数占优: negative_score={negative:.2f}, positive={positive}, neutral={neutral}",
            source="news_signal_cards:counter_evidence",
            as_of=str(
                (sentiment_as_of or {}).get("value")
                or entry.get("as_of")
                or "unknown"
            ),
            metrics={
                "negative_score": negative,
                "positive_score": positive,
                "neutral_score": neutral,
                "evidence_cards": evidence_cards[:3] if isinstance(evidence_cards, list) else [],
            },
        )
        return
    if polarity in {"negative", "deny_or_clarification"}:
        entry = polarity_entry or {}
        _add_item(
            items,
            "negative_event_dominant",
            0.7,
            f"事件极性为 {polarity},负向/否认信息占优",
            source=str(entry.get("detector") or "news"),
            as_of=str(entry.get("as_of") or "unknown"),
            metrics={"polarity": polarity},
        )
        return
    status_entry = index.get("news_sentiment_status")
    status = str((status_entry or {}).get("value") or "").strip().lower()
    if status in {"queried_no_match", "ok"}:
        # The persisted news library was queried for this already-seeded stock;
        # no negative dominance = genuine no hit, not missing data.
        return
    if status == "unavailable":
        reason_entry = index.get("news_sentiment_reason")
        _add_unknown(
            unknowns,
            "negative_event_dominant",
            "新闻情绪库不可用: "
            + str((reason_entry or {}).get("value") or "query_unavailable"),
            source=str((status_entry or {}).get("detector") or "news_counter_evidence"),
            as_of=str((status_entry or {}).get("as_of") or "unknown"),
        )
        return
    if not news_flags:
        _add_unknown(
            unknowns,
            "negative_event_dominant",
            "当前候选没有 news 类 FeatureFlag,无法判断负向事件",
            source="recall:news",
        )
        return
    if neg_entry is None and polarity_entry is None:
        _add_unknown(
            unknowns,
            "negative_event_dominant",
            "news flag 只有事件文本,无 negative_score/sentiment/polarity",
            source="recall:news",
        )


def _valuation_counter(
    index: Dict[str, Dict[str, Any]],
    items: List[Dict[str, Any]],
    unknowns: List[Dict[str, Any]],
) -> None:
    pe_entry = index.get("pe_ttm") or index.get("pe")
    pb_entry = index.get("pb")
    pe = _num((pe_entry or {}).get("value"))
    pb = _num((pb_entry or {}).get("value"))
    if pe is None and pb is None:
        _add_unknown(
            unknowns,
            "extreme_valuation",
            "候选 metrics 无 pe/pe_ttm/pb,无法判断估值是否极端",
            source="recall:valuation",
        )
        return

    severity = 0.0
    reasons: List[str] = []
    if pe is not None and pe <= 0:
        severity = max(severity, 0.6)
        reasons.append(f"PE={pe:.2f} <= 0(亏损)")
    if pe is not None and pe >= 100:
        severity = max(severity, 0.5)
        reasons.append(f"PE={pe:.2f} >= 100")
    if pb is not None and pb >= 10:
        severity = max(severity, 0.5)
        reasons.append(f"PB={pb:.2f} >= 10")
    if severity <= 0.0:
        return
    entry = pe_entry or pb_entry or {}
    _add_item(
        items,
        "extreme_valuation",
        severity,
        "; ".join(reasons),
        source=str(entry.get("detector") or "recall:valuation"),
        as_of=str(entry.get("as_of") or "unknown"),
        metrics={"pe": pe, "pb": pb},
    )


def build_counter_evidence(
    code: str,
    *,
    row: Any = None,
    fact_sheet: Any = None,
) -> Dict[str, Any]:
    """Build the typed counter-evidence block for one candidate.

    ``row`` is a ``FeatureRow`` (or compatible dict); ``fact_sheet`` defaults to
    ``row.fact_sheet``. All checks are pure functions of already-fetched facts.
    """
    if fact_sheet is None and row is not None:
        fact_sheet = _get(row, "fact_sheet", None)

    flags = _flag_dicts(row)
    index = _metric_index(flags)
    items: List[Dict[str, Any]] = []
    unknowns: List[Dict[str, Any]] = []
    fallback_as_of = str(_get(row, "freshness", "") or "").strip()
    if not fallback_as_of and flags:
        fallback_as_of = str(flags[0].get("as_of") or "").strip()

    _theme_counter(
        index,
        items,
        unknowns,
        theme_profile=_get(row, "theme_profile", None),
        fallback_as_of=fallback_as_of,
    )
    _missing_data_counter(fact_sheet, items, unknowns)
    _overextended_counter(fact_sheet, index, items, unknowns)
    _weak_technical_counter(fact_sheet, items, unknowns)
    _negative_event_counter(flags, index, items, unknowns)
    _valuation_counter(index, items, unknowns)

    items.sort(key=lambda item: (-float(item.get("severity") or 0.0), str(item.get("type") or "")))
    max_severity = max([float(item.get("severity") or 0.0) for item in items] or [0.0])
    veto_reasons = sorted(
        {
            str(item.get("type"))
            for item in items
            if (
                str(item.get("type")) in VETO_TYPES
                and float(item.get("severity") or 0.0) >= VETO_TYPE_SEVERITY
            )
            or float(item.get("severity") or 0.0) >= ANY_TYPE_VETO_SEVERITY
        }
    )
    return {
        "code": str(code or ""),
        "items": items,
        "max_severity": round(max_severity, 3),
        "veto_eligible": bool(veto_reasons),
        "veto_reasons": veto_reasons,
        "unknown_checks": unknowns,
    }


def _risk_from_item(item: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "type": str(item.get("type") or "unknown"),
        "summary": str(item.get("reason") or ""),
        "severity": float(item.get("severity") or 0.0),
        "source": str(item.get("source") or ""),
        "as_of": str(item.get("as_of") or ""),
        "reason": str(item.get("reason") or ""),
    }


def attach_counter_evidence(
    candidates: Iterable[Dict[str, Any]],
    rows: Optional[Sequence[Any]],
    *,
    veto_enabled: bool = False,
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """Attach typed counter-evidence to candidate dicts (record-only by default).

    Returns ``(kept_candidates, summary)``. ``kept_candidates`` preserves the
    input order. Scores/confidence are never modified; only ``counter_evidence``
    and deterministic ``risks`` entries are appended. When ``veto_enabled`` is
    True, candidates with ``veto_eligible=True`` are removed and reported in
    ``summary["vetoed"]``.
    """
    rows_by_code: Dict[str, Any] = {}
    for row in rows or []:
        code = str(_get(row, "code", "") or "").strip()
        if code:
            rows_by_code[code] = row

    candidate_list = list(candidates or [])
    kept: List[Dict[str, Any]] = []
    vetoed: List[Dict[str, Any]] = []
    by_type: Dict[str, Dict[str, Any]] = {}
    unknown_counts: Dict[str, int] = {}
    triggered = 0
    veto_eligible_count = 0

    for candidate in candidate_list:
        code = str(candidate.get("code") or "").strip()
        evidence = build_counter_evidence(code, row=rows_by_code.get(code))
        evidence["policy"] = "veto" if veto_enabled else "record_only"
        evidence["veto"] = bool(veto_enabled and evidence["veto_eligible"])

        enriched = dict(candidate)
        enriched["counter_evidence"] = evidence
        enriched["risks"] = list(candidate.get("risks") or []) + [
            _risk_from_item(item) for item in evidence["items"]
        ]

        if evidence["items"]:
            triggered += 1
        if evidence["veto_eligible"]:
            veto_eligible_count += 1
        for item in evidence["items"]:
            bucket = by_type.setdefault(
                str(item.get("type")),
                {"count": 0, "max_severity": 0.0},
            )
            bucket["count"] += 1
            bucket["max_severity"] = round(
                max(float(bucket["max_severity"]), float(item.get("severity") or 0.0)),
                3,
            )
        for unknown in evidence["unknown_checks"]:
            check = str(unknown.get("check") or "unknown")
            unknown_counts[check] = unknown_counts.get(check, 0) + 1

        if evidence["veto"]:
            vetoed.append({"code": code, "reasons": list(evidence["veto_reasons"])})
            continue
        kept.append(enriched)

    summary: Dict[str, Any] = {
        "enabled": True,
        "veto_enabled": bool(veto_enabled),
        "policy": "veto" if veto_enabled else "record_only",
        "candidate_count": len(candidate_list),
        "kept_count": len(kept),
        "triggered_candidates": triggered,
        "veto_eligible_count": veto_eligible_count,
        "by_type": dict(sorted(by_type.items(), key=lambda item: item[0])),
        "unknown_checks": dict(sorted(unknown_counts.items(), key=lambda item: item[0])),
        "vetoed": vetoed,
    }
    return kept, summary


__all__ = [
    "COUNTER_EVIDENCE_TYPES",
    "VETO_TYPES",
    "VETO_TYPE_SEVERITY",
    "ANY_TYPE_VETO_SEVERITY",
    "build_counter_evidence",
    "attach_counter_evidence",
]
