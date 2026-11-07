from contextlib import redirect_stderr
from io import StringIO
from types import SimpleNamespace
from unittest.mock import patch

from data_provider.tushare_client import TushareCredentialError
from scripts import update_fundamental_candidates


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
