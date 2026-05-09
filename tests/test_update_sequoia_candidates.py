import sqlite3

import pandas as pd

from scripts.update_sequoia_candidates import (
    db_summary,
    init_db,
    prune_symbol_row_limit,
    prune_to_latest_trading_days,
    upsert_rows,
)


def test_update_sequoia_candidates_upserts_and_summarizes(tmp_path):
    db_path = tmp_path / "sequoia_v2.db"
    init_db(str(db_path))

    df = pd.DataFrame(
        [
            {
                "symbol": "600001",
                "date": "2026-01-02",
                "open": 10,
                "high": 11,
                "low": 9,
                "close": 10.5,
                "volume": 1000,
                "turnover": 10_000,
            },
            {
                "symbol": "600001",
                "date": "2026-01-03",
                "open": 11,
                "high": 12,
                "low": 10,
                "close": 11.5,
                "volume": 1100,
                "turnover": 11_000,
            },
        ]
    )

    assert upsert_rows(str(db_path), df) == 2
    assert db_summary(str(db_path)) == (2, 1, "2026-01-02", "2026-01-03")

    replacement = df.iloc[[0]].copy()
    replacement.loc[:, "close"] = 12.5
    assert upsert_rows(str(db_path), replacement) == 1

    with sqlite3.connect(db_path) as conn:
        close = conn.execute(
            "SELECT close FROM stock_daily WHERE symbol = ? AND date = ?",
            ("600001", "2026-01-02"),
        ).fetchone()[0]
    assert close == 12.5
    assert db_summary(str(db_path))[0] == 2


def test_update_sequoia_candidates_prunes_to_latest_dates_and_per_symbol_limit(tmp_path):
    db_path = tmp_path / "sequoia_v2.db"
    init_db(str(db_path))
    rows = []
    for symbol in ("600001", "600002"):
        for day in range(1, 6):
            rows.append(
                {
                    "symbol": symbol,
                    "date": f"2026-01-0{day}",
                    "open": 10 + day,
                    "high": 11 + day,
                    "low": 9 + day,
                    "close": 10.5 + day,
                    "volume": 1000 + day,
                    "turnover": 10_000 + day,
                }
            )
    upsert_rows(str(db_path), pd.DataFrame(rows))

    deleted, cutoff = prune_to_latest_trading_days(str(db_path), 3)

    assert deleted == 4
    assert cutoff == "2026-01-03"
    assert db_summary(str(db_path)) == (6, 2, "2026-01-03", "2026-01-05")

    deleted_by_symbol = prune_symbol_row_limit(str(db_path), 2)

    assert deleted_by_symbol == 2
    assert db_summary(str(db_path)) == (4, 2, "2026-01-04", "2026-01-05")
