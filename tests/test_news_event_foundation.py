# -*- coding: utf-8 -*-
"""News event-study foundation: taxonomy, trade date, refs and settlement."""

from __future__ import annotations

import json
import sqlite3
from datetime import date, timedelta

import pytest

from src.backtest.factor_eval.data import reset_benchmark_series_cache
from src.config import Config
from src.services.news_event_foundation import (
    SCOPE_MULTI,
    SCOPE_NO_STOCK,
    SCOPE_SINGLE,
    build_raw_foundation_fields,
    classify_event_category,
    extract_security_refs,
    load_trading_calendar,
    normalize_code,
)
from src.services.news_signal_outcome_service import (
    REASON_ENTRY_LIMIT_LOCKED,
    STATUS_SETTLED,
    STATUS_UNAVAILABLE,
    settle_news_event_outcomes,
)
from src.storage import DatabaseManager, NewsExtractedEvent, NewsSignalOutcome, StockDaily

SESSION_START = date(2024, 1, 2)
DATES = [SESSION_START + timedelta(days=offset) for offset in range(8)]


@pytest.fixture()
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "test.db"))
    monkeypatch.setattr(Config, "_instance", None, raising=False)
    DatabaseManager.reset_instance()
    reset_benchmark_series_cache()
    manager = DatabaseManager.get_instance()
    try:
        yield manager
    finally:
        DatabaseManager.reset_instance()
        reset_benchmark_series_cache()
        Config._instance = None


def _add_bar(session, code, day, *, open_, close, high=None, low=None):
    session.add(
        StockDaily(
            code=code,
            date=day,
            open=open_,
            high=max(open_, close) * 1.01 if high is None else high,
            low=min(open_, close) * 0.99 if low is None else low,
            close=close,
            volume=1000.0,
            amount=1000.0 * close,
            pct_chg=0.0,
            data_source="test",
        )
    )


def _write_benchmark_cache(directory, rows):
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "benchmark_000985_CSI_20240102_20240109.csv"
    with path.open("w", encoding="utf-8") as handle:
        handle.write("trade_date,open,close\n")
        for day, open_, close in rows:
            handle.write(f"{day.strftime('%Y%m%d')},{open_},{close}\n")
    return path


def _write_adj_db(path, code, factors):
    conn = sqlite3.connect(path)
    try:
        conn.execute("CREATE TABLE stock_adj_factors (code TEXT, date TEXT, adj_factor REAL)")
        conn.executemany(
            "INSERT INTO stock_adj_factors (code, date, adj_factor) VALUES (?, ?, ?)",
            [(code, day.isoformat(), value) for day, value in factors.items()],
        )
        conn.commit()
    finally:
        conn.close()


def _add_event(session, *, event_id, code, trade_date, scope=SCOPE_SINGLE):
    session.add(
        NewsExtractedEvent(
            event_id=event_id,
            raw_episode_id="ep-1",
            card_id="card-1",
            signal_date=trade_date,
            event_time=None,
            event_type="消息催化",
            trigger="test",
            subject="测试",
            evidence_sentence="测试事件",
            source_url="",
            source="cls_telegraph",
            extractor="rule_fallback",
            confidence=0.8,
            verification_status="source_only",
            entity_links_json="[]",
            stock_codes_json=json.dumps([{"code": code, "name": "", "source": "text_code"}]),
            stock_scope=scope,
            primary_stock_code=code if scope == SCOPE_SINGLE else None,
            trade_date=trade_date,
        )
    )


def test_normalize_code_and_refs():
    assert normalize_code("600519.SH") == "600519"
    assert normalize_code("sz000001") == "000001"
    assert normalize_code("2026") is None
    assert normalize_code("100000") is None

    refs = extract_security_refs(text="贵州茅台(600519)与000001共同公告")
    assert refs["codes"] == ["000001", "600519"]
    assert refs["scope"] == SCOPE_MULTI

    single = extract_security_refs(text="公告提到600519")
    assert single["scope"] == SCOPE_SINGLE

    empty = extract_security_refs(text="没有证券代码的新闻")
    assert empty["scope"] == SCOPE_NO_STOCK


def test_classify_fine_event_categories():
    assert classify_event_category("2026年年度业绩预告：预增50%")["category"] == "业绩预告"
    assert classify_event_category("公司拟减持不超过2%股份")["category"] == "减持"
    assert classify_event_category("拟回购股份并增持")["category"] == "增持回购"
    assert classify_event_category("控股股东质押部分股份")["category"] == "质押"
    assert classify_event_category("收到证监会立案告知书")["category"] == "监管处罚"
    assert classify_event_category("中标5亿元订单")["category"] == "中标订单"
    assert classify_event_category("每10股派3元")["category"] == "分红"
    assert classify_event_category("涉及重大诉讼")["category"] == "诉讼"
    assert classify_event_category("明日起停牌")["category"] == "停复牌"
    assert classify_event_category("重大资产重组预案")["category"] == "重组并购"
    assert classify_event_category("发布年度报告")["category"] == "财报"
    assert classify_event_category("普通行业新闻")["category"] == "其他"

    fallback = classify_event_category("普通行业新闻", coarse_event_type="业绩验证")
    assert fallback["category"] == "财报"
    assert fallback["source"] == "coarse_event_type"


def test_trade_date_rolls_after_close_and_weekend():
    sessions = [date(2024, 1, 2), date(2024, 1, 3), date(2024, 1, 4), date(2024, 1, 5), date(2024, 1, 8)]
    calendar = load_trading_calendar(sessions)
    assert calendar.resolve(published_at="2024-01-02 10:00:00")[0] == sessions[0]
    assert calendar.resolve(published_at="2024-01-02 16:00:00")[0] == sessions[1]
    assert calendar.resolve(published_at="2024-01-06 09:00:00")[0] == sessions[4]
    assert calendar.resolve(published_at="2024-01-08 16:00:00") == (None, "calendar_exhausted")


def test_build_raw_foundation_fields_marks_no_stock():
    calendar = load_trading_calendar(DATES)
    fields = build_raw_foundation_fields(
        {
            "title": "某公司拟减持股份",
            "summary": "",
            "content": "",
            "stocks": [],
            "subjects": [],
            "published_at": "2024-01-02 16:30:00",
            "signal_date": DATES[0],
        },
        calendar=calendar,
    )
    assert fields["stock_scope"] == SCOPE_NO_STOCK
    assert fields["event_category"] == "减持"
    assert fields["trade_date"] == DATES[1]
    assert fields["stock_codes"] == []


def test_settlement_computes_adjusted_excess_and_is_idempotent(db, tmp_path):
    adj_path = tmp_path / "adj.db"
    _write_adj_db(
        adj_path,
        "600519",
        {
            DATES[1]: 1.0,
            DATES[5]: 1.1,
        },
    )
    with db.get_session() as session:
        for index, day in enumerate(DATES):
            _add_bar(session, "600519", day, open_=10.0, close=10.0 + index * 0.1)
        session.flush()
        # overwrite entry/exit prices used by the assertions
        session.query(StockDaily).filter_by(code="600519", date=DATES[1]).update(
            {"open": 10.0, "close": 11.0}
        )
        session.query(StockDaily).filter_by(code="600519", date=DATES[5]).update(
            {"close": 9.5}
        )
        _add_event(session, event_id="event-ok", code="600519", trade_date=DATES[0])
        session.commit()

    benchmark_dir = tmp_path / "benchmark_cache"
    _write_benchmark_cache(
        benchmark_dir,
        [(day, 100.0, 100.0 + index * 0.2) for index, day in enumerate(DATES)],
    )
    result = settle_news_event_outcomes(
        db=db,
        adj_path=str(adj_path),
        horizons=(1, 5),
        benchmark="000985.CSI",
        cost_bps=20.0,
        cache_dir=str(benchmark_dir),
    )
    assert result["settled"] == 2
    assert result["unavailable"] == 0

    with db.get_session() as session:
        rows = {row.horizon: row for row in session.query(NewsSignalOutcome).all()}
    one_day = rows["1"]
    assert one_day.data_status == STATUS_SETTLED
    assert one_day.stock_return_pct == pytest.approx(10.0, abs=1e-4)
    assert one_day.benchmark_return_pct == pytest.approx(0.2, abs=1e-4)
    assert one_day.excess_return_pct == pytest.approx(9.6, abs=1e-4)

    five_day = rows["5"]
    assert five_day.stock_return_pct == pytest.approx(4.5, abs=1e-4)
    assert five_day.excess_return_pct == pytest.approx(3.3, abs=1e-4)

    again = settle_news_event_outcomes(
        db=db,
        adj_path=str(adj_path),
        horizons=(1, 5),
        benchmark="000985.CSI",
        cost_bps=20.0,
        cache_dir=str(benchmark_dir),
    )
    assert again["skipped_existing"] == 2
    assert again["settled"] == 0


def test_settlement_marks_limit_locked_entry_unavailable(db, tmp_path):
    adj_path = tmp_path / "adj.db"
    _write_adj_db(adj_path, "600519", {day: 1.0 for day in DATES})
    with db.get_session() as session:
        for index, day in enumerate(DATES):
            _add_bar(session, "600519", day, open_=10.0, close=10.0 + index * 0.1)
        session.flush()
        session.query(StockDaily).filter_by(code="600519", date=DATES[1]).update(
            {"open": 11.0, "high": 11.0, "low": 11.0, "close": 11.0}
        )
        _add_event(session, event_id="event-locked", code="600519", trade_date=DATES[0])
        session.commit()

    benchmark_dir = tmp_path / "benchmark_cache"
    _write_benchmark_cache(
        benchmark_dir,
        [(day, 100.0, 100.0 + index * 0.2) for index, day in enumerate(DATES)],
    )
    result = settle_news_event_outcomes(
        db=db,
        adj_path=str(adj_path),
        horizons=(1,),
        benchmark="000985.CSI",
        cache_dir=str(benchmark_dir),
    )
    assert result["unavailable"] == 1
    assert result["unavailable_reasons"][REASON_ENTRY_LIMIT_LOCKED] == 1

    with db.get_session() as session:
        row = session.query(NewsSignalOutcome).one()
    assert row.data_status == STATUS_UNAVAILABLE
    assert row.excess_return_pct is None
