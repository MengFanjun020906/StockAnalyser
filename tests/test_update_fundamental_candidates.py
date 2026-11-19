from contextlib import redirect_stderr
from io import StringIO
from types import SimpleNamespace
from unittest.mock import patch

from data_provider.tushare_client import TushareCredentialError
from scripts import update_fundamental_candidates


def _args(db_path) -> SimpleNamespace:
    return SimpleNamespace(
        db_path=str(db_path),
        limit=300,
        period="",
        batch_size=80,
        progress_every=10,
        no_progress=True,
        resume=True,
        force=False,
        flush_every=20,
    )


def test_main_treats_pending_code_without_fina_indicator_as_non_fatal(tmp_path) -> None:
    stderr = StringIO()
    with patch.object(
        update_fundamental_candidates,
        "parse_args",
        return_value=_args(tmp_path / "fundamental.db"),
    ), patch.object(
        update_fundamental_candidates,
        "get_tushare_token",
        return_value="test-token",
    ), patch.object(
        update_fundamental_candidates,
        "ensure_fundamental_candidate_schema",
        return_value=str(tmp_path / "fundamental.db"),
    ), patch.object(
        update_fundamental_candidates,
        "_fetch_stock_basic",
        return_value=[{"ts_code": "600999.SH", "name": "新股"}],
    ), patch.object(
        update_fundamental_candidates,
        "_load_existing_codes",
        return_value=set(),
    ), patch.object(
        update_fundamental_candidates,
        "_fetch_latest_fina_indicator",
        return_value=None,
    ), redirect_stderr(stderr):
        exit_code = update_fundamental_candidates.main()

    assert exit_code == 0
    assert "not disclosed yet" in stderr.getvalue()


def test_main_still_fails_when_pending_codes_all_error(tmp_path) -> None:
    with patch.object(
        update_fundamental_candidates,
        "parse_args",
        return_value=_args(tmp_path / "fundamental.db"),
    ), patch.object(
        update_fundamental_candidates,
        "get_tushare_token",
        return_value="test-token",
    ), patch.object(
        update_fundamental_candidates,
        "ensure_fundamental_candidate_schema",
        return_value=str(tmp_path / "fundamental.db"),
    ), patch.object(
        update_fundamental_candidates,
        "_fetch_stock_basic",
        return_value=[{"ts_code": "600999.SH", "name": "新股"}],
    ), patch.object(
        update_fundamental_candidates,
        "_load_existing_codes",
        return_value=set(),
    ), patch.object(
        update_fundamental_candidates,
        "_fetch_latest_fina_indicator",
        side_effect=RuntimeError("boom"),
    ):
        exit_code = update_fundamental_candidates.main()

    assert exit_code == 1


def test_main_reports_invalid_tushare_credential_without_traceback() -> None:
    stderr = StringIO()
    with patch.object(
        update_fundamental_candidates,
        "parse_args",
        return_value=SimpleNamespace(db_path=None, limit=1),
    ), patch.object(
        update_fundamental_candidates,
        "get_tushare_token",
        return_value="expired-token",
    ), patch.object(
        update_fundamental_candidates,
        "ensure_fundamental_candidate_schema",
        return_value="data/stock.db",
    ), patch.object(
        update_fundamental_candidates,
        "_fetch_stock_basic",
        side_effect=TushareCredentialError("Token无效或已过期"),
    ), redirect_stderr(stderr):
        exit_code = update_fundamental_candidates.main()

    assert exit_code == 2
    assert "TUSHARE_TOKEN 无效或已过期" in stderr.getvalue()
    assert "Traceback" not in stderr.getvalue()
