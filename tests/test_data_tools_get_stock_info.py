# -*- coding: utf-8 -*-
"""
Contract tests for get_stock_info tool output semantics.
"""

import os
import sys
import time
import types
import unittest
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import patch

import pandas as pd

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.agent.tools.data_tools import (
    ALL_DATA_TOOLS,
    _handle_get_eastmoney_cjzc_daily,
    _handle_get_stock_disclosure_events,
    _handle_get_stock_business_context,
    _handle_get_stock_info,
)


class _FixedDateTimeBeforeCjzcCutoff(datetime):
    @classmethod
    def now(cls, tz=None):
        return cls(2026, 6, 2, 5, 59, 0, tzinfo=tz)


class _FixedDateTimeAtCjzcCutoff(datetime):
    @classmethod
    def now(cls, tz=None):
        return cls(2026, 6, 2, 6, 0, 0, tzinfo=tz)


def test_get_eastmoney_cjzc_daily_matches_target_trade_date_not_latest_row():
    fake_ak = types.SimpleNamespace(
        stock_info_cjzc_em=lambda: pd.DataFrame([
            {
                "标题": "东方财富财经早餐 6月3日周三",
                "摘要": "机器人板块活跃。",
                "发布时间": "2026-06-03 06:00:00",
                "链接": "https://finance.eastmoney.com/a/latest.html",
            },
            {
                "标题": "东方财富财经早餐 6月2日周二",
                "摘要": "MLCC 涨价，AI服务器需求提升，比亚迪发布重要业务进展。",
                "发布时间": "2026-06-02 06:00:00",
                "链接": "https://finance.eastmoney.com/a/20260602.html",
            },
        ])
    )
    with patch.dict(sys.modules, {"akshare": fake_ak}), patch(
        "src.agent.tools.data_tools._fetch_eastmoney_article_sections",
        return_value={"status": "missing", "sections": [], "errors": []},
    ):
        result = _handle_get_eastmoney_cjzc_daily(target_date="2026-06-02")

    assert result["status"] == "ok"
    assert result["trade_date"] == "2026-06-02"
    assert result["matched_publish_date"] == "2026-06-02"
    assert result["session"] == "pre_market_daily"
    assert result["link"].endswith("20260602.html")
    assert "MLCC" in [item["theme"] for item in result["themes"]]
    mlcc = next(item for item in result["themes"] if item["theme"] == "MLCC")
    assert mlcc["mapped_stocks"][0]["code"] == "300408"
    assert result["mentioned_stocks"] == []
    assert "get_eastmoney_cjzc_daily" in {tool.name for tool in ALL_DATA_TOOLS}


class _FakeResponse:
    def __init__(self, payload=None, text="", content=None, headers=None, status_code=200):
        self._payload = payload
        self.text = text
        self.content = content if content is not None else text.encode("utf-8")
        self.headers = headers or {"Content-Type": "text/html; charset=utf-8"}
        self.status_code = status_code
        self.encoding = "utf-8"

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._payload


def test_get_stock_disclosure_events_extracts_cninfo_ir_and_body_terms():
    payload = {
        "announcements": [
            {
                "secCode": "688126",
                "secName": "沪硅产业",
                "announcementTitle": "沪硅产业：投资者关系活动记录表",
                "announcementTime": 1780070400000,
                "adjunctUrl": "new/disclosure/detail.html",
            },
            {
                "secCode": "688126",
                "secName": "沪硅产业",
                "announcementTitle": "沪硅产业：2025年年度报告",
                "announcementTime": 1776355200000,
                "adjunctUrl": "new/disclosure/report.html",
            },
        ]
    }

    def fake_get(url, **_kwargs):
        return _FakeResponse(text=(
            "<div>公司存储用抛光片占产品比例约60-65%，"
            "已建成12英寸SOI硅片中试线，2025年底300mm半导体硅片合计产能达85万片/月。</div>"
        ))

    with patch("src.agent.tools.data_tools.requests.post", return_value=_FakeResponse(payload=payload)), patch(
        "src.agent.tools.data_tools.requests.get",
        side_effect=fake_get,
    ):
        result = _handle_get_stock_disclosure_events(
            "688126",
            "沪硅产业",
            start_date="2026-04-01",
            end_date="2026-06-04",
            include_body=True,
        )

    assert result["status"] == "ok"
    assert result["event_count"] == 2
    assert result["items"][0]["doc_type"] == "investor_relation"
    assert {"存储用抛光片", "SOI", "300mm", "85万片/月"}.issubset(set(result["items"][0]["matched_terms"]))
    assert "storage_material" in result["items"][0]["matched_groups"]
    assert "soi_silicon_photonics" in result["items"][0]["matched_groups"]
    assert "capacity_300mm" in result["items"][0]["matched_groups"]
    assert "get_stock_disclosure_events" in {tool.name for tool in ALL_DATA_TOOLS}


def test_get_stock_disclosure_events_keeps_pdf_metadata_when_body_skipped():
    payload = {
        "announcements": [
            {
                "secCode": "688126",
                "secName": "沪硅产业",
                "announcementTitle": "沪硅产业：2025年年度报告",
                "announcementTime": 1776355200000,
                "adjunctUrl": "new/disclosure/report.pdf",
            }
        ]
    }
    with patch("src.agent.tools.data_tools.requests.post", return_value=_FakeResponse(payload=payload)):
        result = _handle_get_stock_disclosure_events("688126", "沪硅产业", include_body=False)

    assert result["status"] == "ok"
    assert result["events"][0]["doc_type"] == "annual_report"
    assert result["events"][0]["body_status"] == "not_requested"
    assert result["events"][0]["url"].startswith("https://static.cninfo.com.cn/")


def test_get_stock_disclosure_events_surfaces_cninfo_failure():
    with patch("src.agent.tools.data_tools.requests.post", side_effect=RuntimeError("network down")):
        result = _handle_get_stock_disclosure_events("688126", "沪硅产业")

    assert result["status"] == "failed"
    assert result["items"] == []
    assert result["events"] == []
    assert "network down" in result["errors"][0]


def test_get_eastmoney_cjzc_daily_before_6_uses_previous_daily_for_today_request():
    fake_ak = types.SimpleNamespace(
        stock_info_cjzc_em=lambda: pd.DataFrame([
            {
                "标题": "东方财富财经早餐 6月2日周二",
                "摘要": "机器人板块活跃。",
                "发布时间": "2026-06-02 06:00:00",
                "链接": "https://finance.eastmoney.com/a/20260602.html",
            },
            {
                "标题": "东方财富财经早餐 6月1日周一",
                "摘要": "MLCC 涨价，AI服务器需求提升。",
                "发布时间": "2026-06-01 06:00:00",
                "链接": "https://finance.eastmoney.com/a/20260601.html",
            },
        ])
    )
    with patch.dict(sys.modules, {"akshare": fake_ak}), patch(
        "src.agent.tools.data_tools.datetime",
        _FixedDateTimeBeforeCjzcCutoff,
    ), patch(
        "src.agent.tools.data_tools._fetch_eastmoney_article_sections",
        return_value={"status": "missing", "sections": [], "errors": []},
    ):
        result = _handle_get_eastmoney_cjzc_daily(target_date="2026-06-02")

    assert result["status"] == "ok"
    assert result["requested_target_date"] == "2026-06-02"
    assert result["target_date"] == "2026-06-01"
    assert result["trade_date"] == "2026-06-01"
    assert result["matched_publish_date"] == "2026-06-01"
    assert result["target_date_rule"] == "pre_6_use_previous_daily"
    assert result["link"].endswith("20260601.html")


def test_get_eastmoney_cjzc_daily_at_6_uses_today_daily_for_today_request():
    fake_ak = types.SimpleNamespace(
        stock_info_cjzc_em=lambda: pd.DataFrame([
            {
                "标题": "东方财富财经早餐 6月2日周二",
                "摘要": "MLCC 涨价，AI服务器需求提升。",
                "发布时间": "2026-06-02 06:00:00",
                "链接": "https://finance.eastmoney.com/a/20260602.html",
            },
            {
                "标题": "东方财富财经早餐 6月1日周一",
                "摘要": "机器人板块活跃。",
                "发布时间": "2026-06-01 06:00:00",
                "链接": "https://finance.eastmoney.com/a/20260601.html",
            },
        ])
    )
    with patch.dict(sys.modules, {"akshare": fake_ak}), patch(
        "src.agent.tools.data_tools.datetime",
        _FixedDateTimeAtCjzcCutoff,
    ), patch(
        "src.agent.tools.data_tools._fetch_eastmoney_article_sections",
        return_value={"status": "missing", "sections": [], "errors": []},
    ):
        result = _handle_get_eastmoney_cjzc_daily(target_date="2026-06-02")

    assert result["status"] == "ok"
    assert result["requested_target_date"] == "2026-06-02"
    assert result["target_date"] == "2026-06-02"
    assert result["trade_date"] == "2026-06-02"
    assert result["matched_publish_date"] == "2026-06-02"
    assert result["target_date_rule"] == "post_6_use_today_daily"
    assert result["link"].endswith("20260602.html")


def test_get_eastmoney_cjzc_daily_before_6_keeps_historical_replay_exact():
    fake_ak = types.SimpleNamespace(
        stock_info_cjzc_em=lambda: pd.DataFrame([
            {
                "标题": "东方财富财经早餐 6月1日周一",
                "摘要": "MLCC 涨价，AI服务器需求提升。",
                "发布时间": "2026-06-01 06:00:00",
                "链接": "https://finance.eastmoney.com/a/20260601.html",
            },
        ])
    )
    with patch.dict(sys.modules, {"akshare": fake_ak}), patch(
        "src.agent.tools.data_tools.datetime",
        _FixedDateTimeBeforeCjzcCutoff,
    ), patch(
        "src.agent.tools.data_tools._fetch_eastmoney_article_sections",
        return_value={"status": "missing", "sections": [], "errors": []},
    ):
        result = _handle_get_eastmoney_cjzc_daily(target_date="2026-06-01")

    assert result["status"] == "ok"
    assert result["requested_target_date"] == "2026-06-01"
    assert result["target_date"] == "2026-06-01"
    assert result["target_date_rule"] == "historical_replay_exact"
    assert result["link"].endswith("20260601.html")


def test_get_eastmoney_cjzc_daily_missing_does_not_fallback_without_flag():
    fake_ak = types.SimpleNamespace(
        stock_info_cjzc_em=lambda: pd.DataFrame([
            {
                "标题": "东方财富财经早餐 6月1日周一",
                "摘要": "绿色电力。",
                "发布时间": "2026-06-01 06:00:00",
                "链接": "https://finance.eastmoney.com/a/20260601.html",
            },
        ])
    )
    with patch.dict(sys.modules, {"akshare": fake_ak}), patch(
        "src.agent.tools.data_tools._fetch_eastmoney_article_sections",
        return_value={"status": "missing", "sections": [], "errors": []},
    ):
        result = _handle_get_eastmoney_cjzc_daily(target_date="2026-06-02")

    assert result["status"] == "missing"
    assert result["trade_date"] == "2026-06-02"
    assert result["items"] == []


def test_get_eastmoney_cjzc_daily_does_not_treat_brand_or_theme_as_stock():
    fake_ak = types.SimpleNamespace(
        stock_info_cjzc_em=lambda: pd.DataFrame([
            {
                "标题": "东方财富财经早餐 6月2日周二",
                "摘要": "东方财富财经早餐：宇树科技冲刺人形机器人第一股，机器人产业链受关注。",
                "发布时间": "2026-06-02 06:00:00",
                "链接": "https://finance.eastmoney.com/a/20260602.html",
            },
        ])
    )
    with patch.dict(sys.modules, {"akshare": fake_ak}), patch(
        "src.agent.tools.data_tools._fetch_eastmoney_article_sections",
        return_value={"status": "missing", "sections": [], "errors": []},
    ):
        result = _handle_get_eastmoney_cjzc_daily(target_date="2026-06-02")

    assert result["status"] == "ok"
    assert "人形机器人" in [item["theme"] for item in result["themes"]]
    assert result["mentioned_stocks"] == []


def test_get_eastmoney_cjzc_daily_extracts_concepts_and_company_diagnostics_only():
    fake_ak = types.SimpleNamespace(
        stock_info_cjzc_em=lambda: pd.DataFrame([
            {
                "标题": "东方财富财经早餐 6月2日周二",
                "摘要": "MLCC 涨价，AI服务器需求提升。春秋电子：公司不涉及机器人业务，无规模化供应。",
                "发布时间": "2026-06-02 06:00:00",
                "链接": "https://finance.eastmoney.com/a/20260602.html",
            },
        ])
    )
    with patch.dict(sys.modules, {"akshare": fake_ak}), patch(
        "src.agent.tools.data_tools._fetch_eastmoney_article_sections",
        return_value={"status": "missing", "sections": [], "errors": []},
    ):
        result = _handle_get_eastmoney_cjzc_daily(target_date="2026-06-02")

    mlcc = next(item for item in result["themes"] if item["theme"] == "MLCC")
    assert mlcc["polarity"] == "positive"
    assert mlcc["mapped_stocks"][1]["code"] == "000636"
    assert result["mentioned_stocks"] == []
    assert result["company_events"][0]["name"] == "春秋电子"
    assert result["company_events"][0]["polarity"] == "deny_or_clarification"
    assert result["company_events"][0]["seed_allowed"] is False


def test_get_eastmoney_cjzc_daily_reads_article_body_not_only_summary():
    fake_ak = types.SimpleNamespace(
        stock_info_cjzc_em=lambda: pd.DataFrame([
            {
                "标题": "东方财富财经早餐 6月2日周二",
                "摘要": "宇树科技IPO过会，冲刺A股人形机器人第一股。",
                "发布时间": "2026-06-02 06:00:00",
                "链接": "https://finance.eastmoney.com/a/20260602.html",
            },
        ])
    )
    article_sections = [
        {
            "section": "每日精选",
            "text": "MLCC ：高盛指出，在AI服务器物料清单中，MLCC上升为第三大成本项。该领域真正的瓶颈在于产能扩张刚性。",
        },
        {
            "section": "热点题材",
            "text": "宇树科技IPO过会，冲刺A股人形机器人第一股。",
        },
        {
            "section": "公司新闻",
            "text": "春秋电子 ：公司不涉及机器人业务，无规模化供应。",
        },
    ]
    with patch.dict(sys.modules, {"akshare": fake_ak}), patch(
        "src.agent.tools.data_tools._fetch_eastmoney_article_sections",
        return_value={"status": "ok", "sections": article_sections, "text_length": 92, "errors": []},
    ):
        result = _handle_get_eastmoney_cjzc_daily(target_date="2026-06-02")

    themes = [item["theme"] for item in result["themes"]]
    assert themes[0] == "MLCC"
    assert "人形机器人" in themes
    mlcc = result["themes"][0]
    assert mlcc["evidence_section"] == "每日精选"
    assert mlcc["polarity"] == "positive"
    assert mlcc["mapped_stocks"][1]["code"] == "000636"
    assert "AI服务器" in mlcc["high_impact_terms"]
    assert result["article_fetch_status"] == "ok"
    assert result["article_sections"][0]["section"] == "每日精选"
    assert result["company_events"][0]["polarity"] == "deny_or_clarification"


def test_get_eastmoney_cjzc_daily_boosts_nvidia_related_theme_but_not_denial_company_news():
    fake_ak = types.SimpleNamespace(
        stock_info_cjzc_em=lambda: pd.DataFrame([
            {
                "标题": "东方财富财经早餐 6月1日周一",
                "摘要": "热点题材摘要。",
                "发布时间": "2026-06-01 06:00:00",
                "链接": "https://finance.eastmoney.com/a/20260601.html",
            },
        ])
    )
    article_sections = [
        {
            "section": "热点题材",
            "text": "AI电脑：英伟达CEO黄仁勋表示，搭载Blackwell架构的新一代AI电脑即将发布。",
        },
        {
            "section": "公司新闻",
            "text": "风华高科 ：公司关注到媒体提及公司通过英伟达全系列MLCC认证，经核查以上信息不属实，英伟达未开展任何产品认证。",
        },
    ]
    with patch.dict(sys.modules, {"akshare": fake_ak}), patch(
        "src.agent.tools.data_tools._fetch_eastmoney_article_sections",
        return_value={"status": "ok", "sections": article_sections, "text_length": 110, "errors": []},
    ):
        result = _handle_get_eastmoney_cjzc_daily(target_date="2026-06-01")

    assert [item["theme"] for item in result["themes"]] == ["AI PC"]
    ai_pc = result["themes"][0]
    assert ai_pc["high_impact_terms"] == ["英伟达", "黄仁勋", "Blackwell"]
    assert ai_pc["theme_score"] > 20
    assert all(item["theme"] != "MLCC" for item in result["themes"])
    assert result["company_events"][0]["name"] == "风华高科"
    assert result["company_events"][0]["polarity"] == "deny_or_clarification"


def test_get_eastmoney_cjzc_daily_splits_multiple_company_news_events():
    fake_ak = types.SimpleNamespace(
        stock_info_cjzc_em=lambda: pd.DataFrame([
            {
                "标题": "东方财富财经早餐 6月1日周一",
                "摘要": "公司新闻摘要。",
                "发布时间": "2026-06-01 06:00:00",
                "链接": "https://finance.eastmoney.com/a/20260601.html",
            },
        ])
    )
    article_sections = [
        {
            "section": "公司新闻",
            "text": "SpaceX ：马斯克否认IPO估值下调报道。 风华高科 ：公司关注到媒体提及公司通过英伟达全系列MLCC认证，经核查以上信息不属实，英伟达未开展任何产品认证。 精测电子 ：累计签订多份销售合同。",
        }
    ]
    with patch.dict(sys.modules, {"akshare": fake_ak}), patch(
        "src.agent.tools.data_tools._fetch_eastmoney_article_sections",
        return_value={"status": "ok", "sections": article_sections, "text_length": 120, "errors": []},
    ):
        result = _handle_get_eastmoney_cjzc_daily(target_date="2026-06-01")

    events = {item["name"]: item for item in result["company_events"]}
    assert set(events) == {"SpaceX", "风华高科", "精测电子"}
    assert events["风华高科"]["polarity"] == "deny_or_clarification"
    assert events["风华高科"]["seed_allowed"] is False


class _DummyManager:
    def __init__(self):
        self._context = {
            "market": "cn",
            "status": "partial",
            "coverage": {
                "valuation": "ok",
                "growth": "not_supported",
                "earnings": "not_supported",
                "institution": "not_supported",
                "capital_flow": "not_supported",
                "dragon_tiger": "not_supported",
                "boards": "ok",
            },
            "valuation": {
                "status": "ok",
                "data": {
                    "pe_ratio": 12.3,
                    "pb_ratio": 2.1,
                    "total_mv": 1.0e11,
                    "circ_mv": 7.0e10,
                },
            },
            "growth": {"status": "not_supported", "data": {}},
            "earnings": {"status": "not_supported", "data": {}},
            "institution": {"status": "not_supported", "data": {}},
            "capital_flow": {"status": "not_supported", "data": {}},
            "dragon_tiger": {"status": "not_supported", "data": {}},
            "boards": {
                "status": "ok",
                "data": {
                    "top": [{"name": "白酒", "change_pct": 2.3}],
                    "bottom": [{"name": "煤炭", "change_pct": -1.7}],
                },
            },
        }
        self._belong_boards = [{"name": "白酒"}, {"name": "消费"}]

    def get_fundamental_context(self, _stock_code: str, budget_seconds=None):
        return self._context

    def build_failed_fundamental_context(self, _stock_code: str, _reason: str):
        return {
            "market": "cn",
            "status": "failed",
            "coverage": {},
            "valuation": {"status": "failed", "data": {}},
            "growth": {"status": "failed", "data": {}},
            "earnings": {"status": "failed", "data": {}},
            "institution": {"status": "failed", "data": {}},
            "capital_flow": {"status": "failed", "data": {}},
            "dragon_tiger": {"status": "failed", "data": {}},
            "boards": {"status": "failed", "data": {}},
        }

    def get_belong_boards(self, _stock_code: str):
        return self._belong_boards

    def get_stock_name(self, _stock_code: str):
        return "贵州茅台"


class _SlowBoardsManager(_DummyManager):
    def _run_with_timeout(self, task, timeout_seconds, task_name):
        return None, f"{task_name} timeout", int(float(timeout_seconds) * 1000)

    def get_belong_boards(self, _stock_code: str):
        time.sleep(10)
        return [{"name": "不应该等待"}]


class _SlowFundamentalManager(_DummyManager):
    def get_fundamental_context(self, _stock_code: str, budget_seconds=None):
        time.sleep(0.2)
        return self._context


class TestGetStockInfoContract(unittest.TestCase):
    def test_get_stock_info_preserves_board_semantics(self) -> None:
        manager = _DummyManager()
        with patch("src.agent.tools.data_tools._get_fetcher_manager", return_value=manager):
            result = _handle_get_stock_info("600519")

        self.assertEqual(result["name"], "贵州茅台")
        self.assertEqual(result["code"], "600519")
        self.assertEqual(result["pe_ratio"], 12.3)
        self.assertEqual(result["pb_ratio"], 2.1)

        # Contract: boards is compatibility alias of belong_boards.
        self.assertEqual(result["belong_boards"], manager._belong_boards)
        self.assertEqual(result["boards"], result["belong_boards"])

        # Contract: sector_rankings comes from fundamental_context.boards.data.
        self.assertEqual(result["sector_rankings"], manager._context["boards"]["data"])
        self.assertEqual(
            result["fundamental_context"]["boards"]["data"],
            result["sector_rankings"],
        )

    def test_get_stock_info_bounds_optional_belong_boards(self) -> None:
        manager = _SlowBoardsManager()
        with patch("src.agent.tools.data_tools._get_fetcher_manager", return_value=manager):
            result = _handle_get_stock_info("600519")

        self.assertEqual(result["name"], "贵州茅台")
        self.assertEqual(result["belong_boards"], [])
        self.assertIn("belong_boards timeout", result["belong_boards_errors"])
        self.assertEqual(result["belong_boards_source_chain"][0]["result"], "timeout")

    def test_get_stock_info_bounds_fundamental_context_and_keeps_boards(self) -> None:
        manager = _SlowFundamentalManager()
        cfg = SimpleNamespace(fundamental_stage_timeout_seconds=0.01, agent_stock_info_boards_timeout_seconds=1.0)
        with patch("src.agent.tools.data_tools._get_fetcher_manager", return_value=manager), \
             patch("src.config.get_config", return_value=cfg):
            result = _handle_get_stock_info("600519")

        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["belong_boards"], manager._belong_boards)
        self.assertEqual(result["fundamental_source_chain"][0]["result"], "timeout")
        self.assertIn("get_stock_info.fundamental_context timeout", result["fundamental_errors"])

    def test_get_stock_business_context_returns_lightweight_board_context(self) -> None:
        manager = _DummyManager()
        manager._belong_boards = [
            {"name": "电子元件", "code": "BK1036", "type": "行业"},
            {"name": "MLCC", "code": "BKMLCC", "type": "概念"},
            {"name": "AI服务器", "code": "BKAI", "type": "概念"},
        ]
        with patch("src.agent.tools.data_tools._get_fetcher_manager", return_value=manager):
            result = _handle_get_stock_business_context("000636")

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["code"], "000636")
        self.assertEqual(result["name"], "贵州茅台")
        self.assertEqual(result["industry"], "电子元件")
        self.assertEqual(result["boards"], ["电子元件", "MLCC", "AI服务器"])
        self.assertIn("电子元件", result["business_summary"])
        self.assertEqual(result["source"], "data_fetcher:get_belong_boards")
        self.assertNotIn("fundamental_context", result)
        self.assertRegex(result["as_of"], r"^\d{4}-\d{2}-\d{2}$")


if __name__ == "__main__":
    unittest.main()
