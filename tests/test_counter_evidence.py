# -*- coding: utf-8 -*-
"""FAC-02 typed counter-evidence tests."""

from src.agent.candidate_experts_v2.counter_evidence import (
    COUNTER_EVIDENCE_TYPES,
    attach_counter_evidence,
    build_counter_evidence,
)
from src.agent.candidate_experts_v2.schemas import FactSheet, FeatureFlag, FeatureRow, SeedItem
from src.agent.theme_momentum import apply_theme_profile_to_seed


def _healthy_sheet(code: str = "600000", **overrides) -> FactSheet:
    payload = {
        "code": code,
        "trend_state": "bullish",
        "rsi14": 60.0,
        "volume_ratio": 1.3,
        "range_pct_60": 0.55,
        "gain_5d": 3.0,
        "bias_ma20": 4.0,
        "freshness": "local_phase_a",
    }
    payload.update(overrides)
    return FactSheet(**payload)


def _row(code: str, sheet=None, flags=None) -> FeatureRow:
    return FeatureRow(code=code, fact_sheet=sheet, flags=list(flags or []))


def _types(evidence):
    return [item["type"] for item in evidence["items"]]


# --- theme mismatch -------------------------------------------------------

def test_theme_mismatch_is_recorded_and_veto_eligible():
    row = _row(
        "600001",
        _healthy_sheet("600001"),
        [
            FeatureFlag(
                detector="sector:strong",
                kind="sector",
                metrics={
                    "stock_role": "unrelated",
                    "theme_status": "unrelated_confirmed",
                    "theme_regime": "theme_risk_off",
                },
                as_of="2026-11-16",
            )
        ],
    )
    evidence = build_counter_evidence("600001", row=row)
    item = next(i for i in evidence["items"] if i["type"] == "theme_mismatch")
    assert item["severity"] == 0.7
    assert item["source"] == "sector:strong"
    assert item["as_of"] == "2026-11-16"
    assert item["metrics"]["theme_status"] == "unrelated_confirmed"
    assert evidence["veto_eligible"] is True
    assert evidence["veto_reasons"] == ["theme_mismatch"]


def test_theme_metrics_nested_in_signal_value_are_supported():
    row = _row(
        "600011",
        _healthy_sheet("600011"),
        [
            FeatureFlag(
                detector="news_theme_daily",
                kind="news",
                metrics={
                    "mainline_momentum_profile": {
                        "stock_role": "unrelated",
                        "theme_status": "unrelated_confirmed",
                        "theme_regime": "unknown",
                    }
                },
                as_of="2026-11-16",
            )
        ],
    )
    evidence = build_counter_evidence("600011", row=row)
    assert "theme_mismatch" in _types(evidence)


def test_missing_theme_profile_is_unknown_not_mismatch():
    evidence = build_counter_evidence("600002", row=_row("600002", _healthy_sheet("600002")))
    assert "theme_mismatch" not in _types(evidence)
    checks = {item["check"] for item in evidence["unknown_checks"]}
    assert "theme_mismatch" in checks


def test_unknown_theme_role_is_reported_as_unknown_data():
    row = _row(
        "600015",
        _healthy_sheet("600015"),
        [FeatureFlag(detector="theme_momentum", kind="sector", metrics={"stock_role": "unknown", "theme_regime": "unknown"})],
    )
    evidence = build_counter_evidence("600015", row=row)
    assert "theme_mismatch" not in _types(evidence)
    assert any(item["check"] == "theme_mismatch" for item in evidence["unknown_checks"])


# --- missing market data --------------------------------------------------

def test_no_fact_sheet_is_missing_market_data_and_veto_eligible():
    evidence = build_counter_evidence("600003", row=_row("600003", None))
    item = next(i for i in evidence["items"] if i["type"] == "missing_market_data")
    assert item["severity"] == 0.7
    assert evidence["veto_eligible"] is True


def test_insufficient_daily_bars_is_veto_eligible():
    sheet = FactSheet(code="600004", warnings=["insufficient_daily_bars"], freshness="local_phase_a")
    evidence = build_counter_evidence("600004", row=_row("600004", sheet))
    item = next(i for i in evidence["items"] if i["type"] == "missing_market_data")
    assert item["severity"] == 0.7
    assert evidence["veto_eligible"] is True


def test_partial_fact_sheet_is_recorded_but_not_veto_eligible():
    sheet = FactSheet(code="600005", warnings=["some_other_warning"], freshness="local_phase_a")
    evidence = build_counter_evidence("600005", row=_row("600005", sheet))
    item = next(i for i in evidence["items"] if i["type"] == "missing_market_data")
    assert item["severity"] == 0.6
    assert evidence["veto_eligible"] is False


# --- overextended price ---------------------------------------------------

def test_overextended_price_uses_fact_sheet_fields():
    sheet = _healthy_sheet("600006", gain_5d=22.0, bias_ma20=16.0, range_pct_60=0.98)
    evidence = build_counter_evidence("600006", row=_row("600006", sheet))
    item = next(i for i in evidence["items"] if i["type"] == "overextended_price")
    assert item["severity"] >= 0.6
    assert item["source"] == "fact_sheet:gain_5d/bias_ma20/range_pct_60"
    assert evidence["veto_eligible"] is False


def test_overextended_check_unknown_when_fact_sheet_lacks_price_fields():
    sheet = FactSheet(code="600007", freshness="unknown")
    evidence = build_counter_evidence("600007", row=_row("600007", sheet))
    checks = {item["check"] for item in evidence["unknown_checks"]}
    assert "overextended_price" in checks


# --- weak technical confirmation -----------------------------------------

def test_bearish_trend_is_weak_technical_confirmation():
    sheet = _healthy_sheet("600008", trend_state="bearish", rsi14=38.0)
    evidence = build_counter_evidence("600008", row=_row("600008", sheet))
    item = next(i for i in evidence["items"] if i["type"] == "weak_technical_confirmation")
    assert item["severity"] == 0.45
    assert "bearish" in item["reason"]


def test_neutral_low_rsi_with_low_volume_is_weak_confirmation():
    sheet = _healthy_sheet("600009", trend_state="neutral", rsi14=42.0, volume_ratio=0.7)
    evidence = build_counter_evidence("600009", row=_row("600009", sheet))
    assert "weak_technical_confirmation" in _types(evidence)


# --- negative event dominant ---------------------------------------------

def test_negative_event_score_dominance_is_recorded():
    row = _row(
        "600010",
        _healthy_sheet("600010"),
        [
            FeatureFlag(
                detector="news:event_impact",
                kind="news",
                metrics={
                    "negative_score": 0.7,
                    "positive_score": 0.1,
                    "neutral_score": 0.2,
                },
                as_of="2026-11-16",
            )
        ],
    )
    evidence = build_counter_evidence("600010", row=row)
    item = next(i for i in evidence["items"] if i["type"] == "negative_event_dominant")
    assert item["severity"] > 0.7
    assert item["metrics"]["negative_score"] == 0.7


def test_news_flag_without_sentiment_fields_is_unknown():
    row = _row(
        "600012",
        _healthy_sheet("600012"),
        [FeatureFlag(detector="news:momentum", kind="news", metrics={"event_name": "火箭发射"}, as_of="2026-11-16")],
    )
    evidence = build_counter_evidence("600012", row=row)
    assert "negative_event_dominant" not in _types(evidence)
    checks = {item["check"] for item in evidence["unknown_checks"]}
    assert "negative_event_dominant" in checks


def test_news_theme_daily_detector_counts_as_news_even_with_unknown_kind():
    row = _row(
        "600016",
        _healthy_sheet("600016"),
        [
            FeatureFlag(
                detector="news_theme_daily",
                kind="unknown",
                metrics={"polarity": "neutral", "theme": "航运"},
                as_of="2026-11-16",
            )
        ],
    )
    evidence = build_counter_evidence("600016", row=row)
    assert "negative_event_dominant" not in _types(evidence)
    checks = {item["check"] for item in evidence["unknown_checks"]}
    assert "negative_event_dominant" not in checks


# --- extreme valuation ----------------------------------------------------

def test_extreme_valuation_from_pe_metrics():
    row = _row(
        "600013",
        _healthy_sheet("600013"),
        [FeatureFlag(detector="valuation:low_pe", kind="fundamental", metrics={"pe": -5.0, "pb": 2.0}, as_of="2026-11-16")],
    )
    evidence = build_counter_evidence("600013", row=row)
    item = next(i for i in evidence["items"] if i["type"] == "extreme_valuation")
    assert item["severity"] == 0.6
    assert "PE" in item["reason"]


def test_no_valuation_metrics_is_unknown():
    evidence = build_counter_evidence("600014", row=_row("600014", _healthy_sheet("600014")))
    checks = {item["check"] for item in evidence["unknown_checks"]}
    assert "extreme_valuation" in checks


# --- attach: default record-only, opt-in veto ----------------------------

def test_attach_default_is_record_only_and_preserves_order():
    good = {"code": "600020", "score": 88, "risks": []}
    bad = {"code": "600021", "score": 90, "risks": []}
    rows = [
        _row("600020", _healthy_sheet("600020")),
        _row(
            "600021",
            _healthy_sheet("600021"),
            [FeatureFlag(detector="sector:strong", kind="sector", metrics={"stock_role": "unrelated", "theme_status": "unrelated_confirmed"}, as_of="2026-11-16")],
        ),
    ]
    kept, summary = attach_counter_evidence([good, bad], rows)
    assert [c["code"] for c in kept] == ["600020", "600021"]
    assert [c["score"] for c in kept] == [88, 90]
    assert kept[1]["counter_evidence"]["policy"] == "record_only"
    assert kept[1]["counter_evidence"]["veto"] is False
    assert summary["policy"] == "record_only"
    assert summary["vetoed"] == []
    assert summary["by_type"]["theme_mismatch"]["count"] == 1
    assert any(r["type"] == "theme_mismatch" for r in kept[1]["risks"])


def test_attach_with_veto_enabled_removes_only_veto_eligible():
    good = {"code": "600030", "score": 88, "risks": []}
    bad = {"code": "600031", "score": 90, "risks": []}
    rows = [
        _row("600030", _healthy_sheet("600030")),
        _row("600031", None),
    ]
    kept, summary = attach_counter_evidence([good, bad], rows, veto_enabled=True)
    assert [c["code"] for c in kept] == ["600030"]
    assert summary["policy"] == "veto"
    assert summary["veto_eligible_count"] == 1
    assert summary["vetoed"] == [{"code": "600031", "reasons": ["missing_market_data"]}]


def test_counter_evidence_type_catalog_is_complete():
    assert set(COUNTER_EVIDENCE_TYPES) == {
        "theme_mismatch",
        "missing_market_data",
        "overextended_price",
        "weak_technical_confirmation",
        "negative_event_dominant",
        "extreme_valuation",
    }


# --- FAC-02 data-gap closures -------------------------------------------

def test_theme_profile_is_persisted_for_unclassified_without_old_signal():
    seed = SeedItem(code="600099", name="测试标的", source="daily_screener")
    apply_theme_profile_to_seed(
        seed,
        {
            "theme": "ai_compute_chain",
            "regime": "theme_risk_off",
            "confidence": 0.8,
            "data_quality": "ok",
            "evidence": ["主题证据走弱"],
        },
    )
    assert isinstance(seed.theme_profile, dict)
    assert seed.theme_profile["theme_status"] == "unclassified"
    assert seed.theme_profile["stock_role"] == "unknown"
    assert seed.extras["theme_profile"]["theme_status"] == "unclassified"
    # Old semantics preserved: unclassified roles still emit no mainline profile signal.
    assert not any(
        signal.get("signal_type") == "mainline_momentum_profile"
        for signal in seed.trigger_signals
    )


def test_unclassified_theme_status_is_recorded_but_not_veto_evidence():
    row = FeatureRow(
        code="600097",
        fact_sheet=_healthy_sheet("600097"),
        theme_profile={
            "theme_status": "unclassified",
            "stock_role": "unknown",
            "theme_regime": "unknown",
            "membership_confidence": 0.0,
            "evidence": ["主题证据不足"],
            "evidence_source": "seed.theme_profile",
            "as_of": "2026-11-16",
            "theme_attribution": {
                "decision": "unclassified",
                "missing_reason": "检查 5 个文本字段未命中主题别名（证据不足，不等于与主题无关）",
                "sources": [],
            },
        },
    )
    evidence = build_counter_evidence("600097", row=row)
    assert "theme_mismatch" not in _types(evidence)
    check = next(item for item in evidence["unknown_checks"] if item["check"] == "theme_mismatch")
    assert check["metrics"]["theme_status"] == "unclassified"
    assert "不判为无关" in check["reason"]
    assert evidence["veto_eligible"] is False


def test_legacy_unrelated_without_theme_status_is_not_veto_evidence():
    row = FeatureRow(
        code="600096",
        fact_sheet=_healthy_sheet("600096"),
        theme_profile={
            "stock_role": "unrelated",
            "theme_regime": "unknown",
            "membership_confidence": 0.0,
            "evidence": ["来源=test，未匹配主题别名"],
            "evidence_source": "seed_fact_packet.theme_profile",
            "as_of": "2026-11-16",
        },
    )
    evidence = build_counter_evidence("600096", row=row)
    assert "theme_mismatch" not in _types(evidence)
    check = next(item for item in evidence["unknown_checks"] if item["check"] == "theme_mismatch")
    assert "缺少 theme_status" in check["reason"]
    assert evidence["veto_eligible"] is False


def test_counter_evidence_reads_explicit_theme_profile_for_unrelated():
    row = FeatureRow(
        code="600098",
        fact_sheet=_healthy_sheet("600098"),
        theme_profile={
            "theme_status": "unrelated_confirmed",
            "stock_role": "unrelated",
            "theme_regime": "theme_risk_off",
            "membership_confidence": 0.0,
            "evidence": ["确认他主题（explicit_other_theme）：航运"],
            "evidence_source": "seed_fact_packet.theme_profile",
            "as_of": "2026-11-16",
            "theme_confirmation": {
                "evidence_kind": "explicit_other_theme",
                "severity": 0.7,
                "labels": [{"field": "metrics.concepts", "value": "航运港口"}],
                "rule": "存在明确他主题/板块/行业标签",
            },
        },
    )
    evidence = build_counter_evidence("600098", row=row)
    item = next(i for i in evidence["items"] if i["type"] == "theme_mismatch")
    assert item["severity"] == 0.7
    assert item["source"] == "seed_fact_packet.theme_profile"
    assert item["as_of"] == "2026-11-16"
    assert item["metrics"]["membership_confidence"] == 0.0
    assert item["metrics"]["theme_status"] == "unrelated_confirmed"
    assert item["metrics"]["evidence_kind"] == "explicit_other_theme"
    assert item["metrics"]["theme_labels"] == ["航运港口"]


def test_theme_mismatch_uses_theme_attribution_missing_reason():
    row = FeatureRow(
        code="600099",
        fact_sheet=_healthy_sheet("600099"),
        theme_profile={
            "theme_status": "unrelated_confirmed",
            "stock_role": "unrelated",
            "theme_regime": "unknown",
            "membership_confidence": 0.0,
            "evidence": ["来源=hot_rank，角色=unrelated，setup=none。"],
            "evidence_source": "seed.theme_profile",
            "as_of": "2026-11-16",
            "theme_attribution": {
                "decision": "unrelated_confirmed",
                "missing_reason": "明确他主题标签：银行；未命中目标主题别名",
                "sources": [
                    {"source_type": "seed_source", "source_id": "hot_rank", "as_of": "2026-11-16"},
                    {"source_type": "seed_text", "source_id": "name", "as_of": "2026-11-16"},
                ],
                "confirmation": {
                    "evidence_kind": "explicit_other_theme",
                    "severity": 0.7,
                    "labels": [{"field": "metrics.concepts", "value": "银行"}],
                },
                "display_only": True,
                "affects_ranking": False,
            },
        },
    )
    evidence = build_counter_evidence("600099", row=row)
    item = next(i for i in evidence["items"] if i["type"] == "theme_mismatch")
    assert item["severity"] == 0.7
    assert "明确他主题标签：银行" in item["reason"]
    assert item["metrics"]["theme_attribution_decision"] == "unrelated_confirmed"
    assert item["metrics"]["theme_attribution_sources"][0]["source_id"] == "hot_rank"


def test_news_queried_no_match_is_genuine_no_hit_not_unknown():
    row = _row(
        "600097",
        _healthy_sheet("600097"),
        [
            FeatureFlag(
                detector="news_counter_evidence",
                kind="news",
                metrics={"news_sentiment_status": "queried_no_match", "news_sentiment_as_of": "2026-11-16"},
            )
        ],
    )
    evidence = build_counter_evidence("600097", row=row)
    assert "negative_event_dominant" not in _types(evidence)
    assert not any(item["check"] == "negative_event_dominant" for item in evidence["unknown_checks"])


def test_news_unavailable_is_reported_as_unknown():
    row = _row(
        "600096",
        _healthy_sheet("600096"),
        [
            FeatureFlag(
                detector="news_counter_evidence",
                kind="news",
                metrics={
                    "news_sentiment_status": "unavailable",
                    "news_sentiment_reason": "database locked",
                },
            )
        ],
    )
    evidence = build_counter_evidence("600096", row=row)
    assert "negative_event_dominant" not in _types(evidence)
    unknown = next(i for i in evidence["unknown_checks"] if i["check"] == "negative_event_dominant")
    assert "database locked" in unknown["reason"]


def test_news_counter_scores_are_auditable_and_include_cards():
    row = _row(
        "600095",
        _healthy_sheet("600095"),
        [
            FeatureFlag(
                detector="news_counter_evidence",
                kind="news",
                metrics={
                    "news_sentiment_status": "ok",
                    "negative_score": 0.86,
                    "positive_score": 0.0,
                    "neutral_score": 0.0,
                    "news_sentiment_as_of": "2026-11-11",
                    "news_counter_evidence": [
                        {"card_id": "card-1", "signal_date": "2026-11-11", "news_tone": "negative"}
                    ],
                },
            )
        ],
    )
    evidence = build_counter_evidence("600095", row=row)
    item = next(i for i in evidence["items"] if i["type"] == "negative_event_dominant")
    assert item["source"] == "news_signal_cards:counter_evidence"
    assert item["as_of"] == "2026-11-11"
    assert item["metrics"]["evidence_cards"][0]["card_id"] == "card-1"


def test_news_counter_evidence_service_ignores_entry_gates():
    from src.services.news_signal_service import NewsSignalService

    class _Repo:
        def list_cards(self, **kwargs):
            return [
                {
                    "card_id": "neg-1",
                    "signal_date": "2026-11-11",
                    "status": "active",
                    "news_tone": "negative",
                    "mapping_status": "mapped",
                    "mapping_confidence": 0.9,
                    "signal_score": 30,
                    "company_impacts": [
                        {"symbol": "600715", "direction": "harm", "confidence": 0.9, "rationale": "立案调查"}
                    ],
                }
            ]

    service = object.__new__(NewsSignalService)
    service.repo = _Repo()
    result = service.counter_evidence_for_codes(["600715"], signal_date="2026-11-16", lookback_days=7)
    assert result["status"] == "ok"
    items = result["items_by_code"]["600715"]
    assert items[0]["company_direction"] == "harm"
    assert items[0]["negative_score"] >= 0.75
    assert items[0]["usage"] == "counter_evidence_only"


def test_news_counter_evidence_service_marks_no_match_after_successful_query():
    from src.services.news_signal_service import NewsSignalService

    class _Repo:
        def list_cards(self, **kwargs):
            return []

    service = object.__new__(NewsSignalService)
    service.repo = _Repo()
    result = service.counter_evidence_for_codes(["600714"], signal_date="2026-11-16")
    assert result["status"] == "ok"
    assert result["items_by_code"] == {}

