from __future__ import annotations

from datetime import date, datetime

import pandas as pd
import pytest

from src.agent.pricing.historical_metadata import (
    historical_cn_common_stock_metadata,
)

FIXED_SESSION = date(2026, 11, 20)
HISTORICAL_AS_OF = date(2023, 12, 1)

STOCK_BASIC_HEADER = [
    "ts_code",
    "name",
    "industry",
    "market",
    "list_date",
    "delist_date",
    "list_status",
]
NAMECHANGE_HEADER = [
    "ts_code",
    "name",
    "start_date",
    "end_date",
    "ann_date",
    "change_reason",
]


def _write_snapshots(
    tmp_path,
    *,
    stock_basic_rows: list[list[str]],
    namechange_rows: list[list[str]] | None,
):
    stock_basic = pd.DataFrame(stock_basic_rows, columns=STOCK_BASIC_HEADER)
    stock_basic.to_csv(tmp_path / "stock_basic_LD.csv", index=False)
    if namechange_rows is not None:
        namechange = pd.DataFrame(namechange_rows, columns=NAMECHANGE_HEADER)
        namechange.to_csv(tmp_path / "namechange_snapshot.csv", index=False)
    return tmp_path


def _pin_current_session(monkeypatch) -> None:
    monkeypatch.setattr(
        "src.core.trading_calendar.get_effective_trading_date",
        lambda *_args, **_kwargs: FIXED_SESSION,
    )
    monkeypatch.setattr(
        "src.core.trading_calendar.get_market_now",
        lambda *_args, **_kwargs: datetime(2026, 11, 20, 15, 0),
    )


@pytest.fixture
def listed_stock(tmp_path):
    """One listed stock whose name went ST in 2023 and was cleared in 2024."""
    return _write_snapshots(
        tmp_path,
        stock_basic_rows=[
            ["600001.SH", "示例股份", "综合", "主板", "20100104", "", "L"],
        ],
        namechange_rows=[
            ["600001.SH", "示例股份", "20100104", "20230430", "20100104", "其他"],
            ["600001.SH", "*ST示例", "20230501", "20240530", "20230429", "ST"],
            ["600001.SH", "示例股份", "20240531", "", "20240530", "撤销ST"],
        ],
    )


def test_current_session_stays_on_live_path(monkeypatch, listed_stock) -> None:
    _pin_current_session(monkeypatch)

    assert (
        historical_cn_common_stock_metadata(
            "600001",
            trade_date=FIXED_SESSION,
            cache_dir=listed_stock,
        )
        is None
    )


def test_historical_date_derives_st_from_namechange(monkeypatch, listed_stock) -> None:
    _pin_current_session(monkeypatch)

    metadata = historical_cn_common_stock_metadata(
        "600001",
        trade_date=HISTORICAL_AS_OF,
        cache_dir=listed_stock,
    )

    assert metadata is not None
    assert metadata["st_status"] is True
    assert metadata["st_status_verified"] is True
    assert metadata["listing_status"] == "listed"
    assert metadata["listing_status_verified"] is True
    assert metadata["instrument_type"] == "common_stock"
    assert metadata["instrument_type_verified"] is True
    assert metadata["metadata_observed_on"] == HISTORICAL_AS_OF


def test_st_removal_is_dated_by_namechange(monkeypatch, listed_stock) -> None:
    _pin_current_session(monkeypatch)

    cleared = historical_cn_common_stock_metadata(
        "600001",
        trade_date=date(2024, 7, 1),
        cache_dir=listed_stock,
    )

    assert cleared is not None
    assert cleared["st_status"] is False
    assert cleared["st_status_verified"] is True


def test_delisted_stock_reports_delisted_status(monkeypatch, tmp_path) -> None:
    _pin_current_session(monkeypatch)
    cache = _write_snapshots(
        tmp_path,
        stock_basic_rows=[
            ["600002.SH", "退市示例", "综合", "主板", "20150105", "20250210", "D"],
        ],
        namechange_rows=[
            ["600002.SH", "退市示例", "20150105", "20250210", "20150105", "其他"],
        ],
    )

    after_delist = historical_cn_common_stock_metadata(
        "600002",
        trade_date=date(2025, 3, 3),
        cache_dir=cache,
    )
    before_delist = historical_cn_common_stock_metadata(
        "600002",
        trade_date=date(2024, 6, 3),
        cache_dir=cache,
    )

    assert after_delist is not None
    assert after_delist["listing_status"] == "delisted"
    assert after_delist["listing_status_verified"] is True
    assert before_delist is not None
    assert before_delist["listing_status"] == "listed"


def test_delisted_without_delist_date_is_not_verified(monkeypatch, tmp_path) -> None:
    _pin_current_session(monkeypatch)
    cache = _write_snapshots(
        tmp_path,
        stock_basic_rows=[
            ["600003.SH", "示例科技", "综合", "主板", "20150105", "", "D"],
        ],
        namechange_rows=[],
    )

    metadata = historical_cn_common_stock_metadata(
        "600003",
        trade_date=HISTORICAL_AS_OF,
        cache_dir=cache,
    )

    assert metadata is not None
    assert metadata["listing_status"] == "unknown"
    assert metadata["listing_status_verified"] is False


def test_missing_stock_basic_row_returns_none(monkeypatch, tmp_path) -> None:
    _pin_current_session(monkeypatch)
    cache = _write_snapshots(tmp_path, stock_basic_rows=[], namechange_rows=[])

    assert (
        historical_cn_common_stock_metadata(
            "600519",
            trade_date=HISTORICAL_AS_OF,
            cache_dir=cache,
        )
        is None
    )


def test_missing_namechange_snapshot_marks_st_unverified(monkeypatch, tmp_path) -> None:
    _pin_current_session(monkeypatch)
    cache = _write_snapshots(
        tmp_path,
        stock_basic_rows=[
            ["600004.SH", "*ST示例", "综合", "主板", "20100104", "", "L"],
        ],
        namechange_rows=None,
    )

    metadata = historical_cn_common_stock_metadata(
        "600004",
        trade_date=HISTORICAL_AS_OF,
        cache_dir=cache,
    )

    assert metadata is not None
    assert metadata["st_status"] is True
    assert metadata["st_status_verified"] is False


def test_no_namechange_records_uses_stock_basic_name_verified(
    monkeypatch, tmp_path
) -> None:
    _pin_current_session(monkeypatch)
    cache = _write_snapshots(
        tmp_path,
        stock_basic_rows=[
            ["600005.SH", "示例股份", "综合", "主板", "20010827", "", "L"],
        ],
        namechange_rows=[],
    )

    metadata = historical_cn_common_stock_metadata(
        "600005",
        trade_date=HISTORICAL_AS_OF,
        cache_dir=cache,
    )

    assert metadata is not None
    assert metadata["st_status"] is False
    assert metadata["st_status_verified"] is True


def test_pre_listing_date_is_not_listed_and_st_unverified(
    monkeypatch, tmp_path
) -> None:
    _pin_current_session(monkeypatch)
    cache = _write_snapshots(
        tmp_path,
        stock_basic_rows=[
            ["600006.SH", "示例股份", "综合", "主板", "20250106", "", "L"],
        ],
        namechange_rows=[
            ["600006.SH", "示例股份", "20250106", "", "20250105", "其他"],
        ],
    )

    metadata = historical_cn_common_stock_metadata(
        "600006",
        trade_date=date(2024, 6, 3),
        cache_dir=cache,
    )

    assert metadata is not None
    assert metadata["st_status_verified"] is False
    assert metadata["listing_status"] == "not_listed"
    assert metadata["listing_status_verified"] is True


def test_readiness_accepts_historical_metadata(monkeypatch, tmp_path) -> None:
    from src.agent.pricing.readiness import PricePlanReadinessGate
    from src.storage import DatabaseManager

    _pin_current_session(monkeypatch)
    cache = _write_snapshots(
        tmp_path,
        stock_basic_rows=[
            ["600001.SH", "示例股份", "综合", "主板", "20100104", "", "L"],
        ],
        namechange_rows=[
            ["600001.SH", "示例股份", "20100104", "20230430", "20100104", "其他"],
            ["600001.SH", "*ST示例", "20230501", "20240530", "20230429", "ST"],
        ],
    )
    monkeypatch.setattr(
        "src.agent.pricing.readiness.current_cn_common_stock_metadata",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        "src.agent.pricing.historical_metadata.DEFAULT_CACHE_DIR",
        cache,
    )
    DatabaseManager.reset_instance()
    db = DatabaseManager(f"sqlite:///{tmp_path / 'historical-readiness.db'}")
    try:
        result = PricePlanReadinessGate(db).evaluate(
            symbol="600001",
            as_of=HISTORICAL_AS_OF,
        )

        assert result.ready is True
        assert result.checks["market_rules"] is True
    finally:
        DatabaseManager.reset_instance()


def test_snapshot_builder_uses_historical_branch(monkeypatch) -> None:
    from src.agent.pricing.snapshot import PricePlanSnapshotBuilder

    frame = pd.DataFrame(
        [
            {
                "date": "2023-11-29",
                "open": 10,
                "high": 11,
                "low": 9,
                "close": 10.5,
                "volume": 100,
                "amount": 1050,
            },
            {
                "date": "2023-11-30",
                "open": 10,
                "high": 11,
                "low": 9,
                "close": 10.5,
                "volume": 100,
                "amount": 1050,
            },
        ]
    )
    history_calls: list[date] = []

    def historical(_symbol: str, *, trade_date: date):
        history_calls.append(trade_date)
        return {
            "st_status": False,
            "st_status_verified": True,
            "listing_status": "listed",
            "listing_status_verified": True,
            "instrument_type": "common_stock",
            "instrument_type_verified": True,
            "metadata_observed_on": trade_date,
        }

    monkeypatch.setattr(
        "src.agent.pricing.snapshot.resolve_completed_cn_as_of",
        lambda *_args, **_kwargs: HISTORICAL_AS_OF,
    )
    monkeypatch.setattr(
        "src.agent.pricing.snapshot.current_cn_common_stock_metadata",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        "src.agent.pricing.snapshot.historical_cn_common_stock_metadata",
        historical,
    )
    snapshot = PricePlanSnapshotBuilder(
        history_loader=lambda *args, **kwargs: (frame, "local"),
        chip_loader=lambda *_args: None,
    ).build("600001", as_of=HISTORICAL_AS_OF)

    assert history_calls == [date(2023, 11, 30)]
    assert snapshot.market_rules.st_status_verified is True
    assert snapshot.market_rules.listing_status == "listed"
    assert snapshot.market_rules.instrument_type == "common_stock"
    assert snapshot.market_rules.trade_date == date(2023, 11, 30)


def test_snapshot_builder_keeps_live_path_for_current_session(monkeypatch) -> None:
    from src.agent.pricing.snapshot import PricePlanSnapshotBuilder

    frame = pd.DataFrame(
        [
            {
                "date": "2026-11-19",
                "open": 10,
                "high": 11,
                "low": 9,
                "close": 10.5,
                "volume": 100,
                "amount": 1050,
            },
        ]
    )

    def unexpected(*_args, **_kwargs):
        raise AssertionError("historical resolver must not run for the live path")

    monkeypatch.setattr(
        "src.agent.pricing.snapshot.resolve_completed_cn_as_of",
        lambda *_args, **_kwargs: FIXED_SESSION,
    )
    monkeypatch.setattr(
        "src.agent.pricing.snapshot.current_cn_common_stock_metadata",
        lambda *_args, **_kwargs: {
            "st_status": False,
            "st_status_verified": True,
            "listing_status": "listed",
            "listing_status_verified": True,
            "instrument_type": "common_stock",
            "instrument_type_verified": True,
            "metadata_observed_on": FIXED_SESSION,
        },
    )
    monkeypatch.setattr(
        "src.agent.pricing.snapshot.historical_cn_common_stock_metadata",
        unexpected,
    )
    snapshot = PricePlanSnapshotBuilder(
        history_loader=lambda *args, **kwargs: (frame, "local"),
        chip_loader=lambda *_args: None,
    ).build("600001", as_of=FIXED_SESSION)

    assert snapshot.market_rules.listing_status_verified is True
    assert snapshot.market_rules.instrument_type == "common_stock"
