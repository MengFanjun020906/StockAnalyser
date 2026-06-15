# -*- coding: utf-8 -*-
"""Contract tests for Tushare-backed Agent data tools."""

from datetime import datetime
from unittest.mock import patch

import pandas as pd

from src.agent.tools.data_tools import (
    ALL_DATA_TOOLS,
    _handle_get_board_capital_flow,
    _handle_get_margin_trading_summary,
    _handle_get_tushare_block_trade,
    _handle_get_tushare_dragon_tiger_inst,
    _handle_get_tushare_dragon_tiger_list,
    _handle_get_tushare_hsgt_top10,
    _handle_get_tushare_adj_factor,
    _handle_get_tushare_announcements,
    _handle_get_tushare_daily_basic,
    _handle_get_tushare_dividend,
    _handle_get_tushare_express,
    _handle_get_tushare_financial_indicators,
    _handle_get_tushare_forecast,
    _handle_get_tushare_holder_trade,
    _handle_get_tushare_hot_rank,
    _handle_get_tushare_index_daily,
    _handle_get_tushare_limit_list_d,
    _handle_get_tushare_limit_list_ths,
    _handle_get_tushare_limit_step,
    _handle_get_tushare_moneyflow_cnt_ths,
    _handle_get_tushare_moneyflow_dc,
    _handle_get_tushare_moneyflow_ind_dc,
    _handle_get_tushare_moneyflow_ind_ths,
    _handle_get_tushare_moneyflow_hsgt,
    _handle_get_tushare_moneyflow_mkt_dc,
    _handle_get_tushare_moneyflow_ths,
    _handle_get_tushare_margin_detail,
    _handle_get_tushare_pledge_detail,
    _handle_get_tushare_pledge_stat,
    _handle_get_tushare_repurchase,
    _handle_get_tushare_share_float,
    _handle_get_tushare_stk_factor,
    _handle_get_tushare_stock_alerts,
    _handle_get_tushare_stock_shock,
    _handle_get_tushare_ths_member,
    _handle_get_tushare_trade_calendar,
    _handle_get_tushare_basic_data,
    _handle_get_tushare_daily_bars,
    _handle_get_tushare_financial_statements,
    _handle_get_tushare_reference_events,
    _handle_get_tushare_today_news,
)


def _fake_query(api_name, params=None, fields="", timeout=30):
    if api_name == "stock_basic":
        return pd.DataFrame([{"ts_code": "603418.SH", "symbol": "603418", "name": "友升股份"}])
    if api_name == "trade_cal":
        return pd.DataFrame([
            {"cal_date": "20260508", "is_open": 1},
            {"cal_date": "20260507", "is_open": 1},
            {"cal_date": "20260506", "is_open": 0},
        ])
    if api_name == "moneyflow_ths":
        trade_date = (params or {}).get("trade_date", "20260508")
        return pd.DataFrame([
            {
                "trade_date": trade_date,
                "ts_code": "600000.SH",
                "name": "浦发银行",
                "pct_change": -1.2,
                "latest": 8.12,
                "net_amount": 10.0,
                "net_d5_amount": 20.0,
                "buy_lg_amount": 6.0,
                "buy_lg_amount_rate": 60.0,
                "buy_md_amount": 2.0,
                "buy_md_amount_rate": 20.0,
                "buy_sm_amount": 2.0,
                "buy_sm_amount_rate": 20.0,
            },
            {
                "trade_date": trade_date,
                "ts_code": "600519.SH",
                "name": "贵州茅台",
                "pct_change": 2.5,
                "latest": 1688.0,
                "net_amount": 20.0,
                "net_d5_amount": 50.0,
                "buy_lg_amount": 12.0,
                "buy_lg_amount_rate": 60.0,
                "buy_md_amount": 4.0,
                "buy_md_amount_rate": 20.0,
                "buy_sm_amount": 4.0,
                "buy_sm_amount_rate": 20.0,
            },
        ])
    if api_name == "moneyflow_dc":
        trade_date = (params or {}).get("trade_date", "20260508")
        return pd.DataFrame([
            {
                "trade_date": trade_date,
                "ts_code": "600000.SH",
                "name": "浦发银行",
                "pct_change": -1.0,
                "close": 8.12,
                "net_amount": 12.0,
                "net_amount_rate": 6.0,
                "buy_elg_amount": 4.0,
                "buy_elg_amount_rate": 2.0,
                "buy_lg_amount": 8.0,
                "buy_lg_amount_rate": 4.0,
                "buy_md_amount": -2.0,
                "buy_md_amount_rate": -1.0,
                "buy_sm_amount": -10.0,
                "buy_sm_amount_rate": -5.0,
            },
            {
                "trade_date": trade_date,
                "ts_code": "600519.SH",
                "name": "贵州茅台",
                "pct_change": 3.0,
                "close": 1688.0,
                "net_amount": 30.0,
                "net_amount_rate": 9.0,
                "buy_elg_amount": 20.0,
                "buy_elg_amount_rate": 6.0,
                "buy_lg_amount": 10.0,
                "buy_lg_amount_rate": 3.0,
                "buy_md_amount": -5.0,
                "buy_md_amount_rate": -2.0,
                "buy_sm_amount": -25.0,
                "buy_sm_amount_rate": -7.0,
            },
        ])
    if api_name == "moneyflow_ind_ths":
        trade_date = (params or {}).get("trade_date", "20260508")
        return pd.DataFrame([
            {
                "trade_date": trade_date,
                "ts_code": "881001.TI",
                "industry": "银行",
                "lead_stock": "浦发银行",
                "close": 1000.0,
                "pct_change": 1.2,
                "company_num": 42,
                "pct_change_stock": 10.0,
                "close_price": 8.12,
                "net_buy_amount": 1.0,
                "net_sell_amount": 0.2,
                "net_amount": 0.8,
            },
            {
                "trade_date": trade_date,
                "ts_code": "881002.TI",
                "industry": "白酒",
                "lead_stock": "贵州茅台",
                "close": 2000.0,
                "pct_change": 2.5,
                "company_num": 20,
                "pct_change_stock": 9.0,
                "close_price": 1688.0,
                "net_buy_amount": 3.0,
                "net_sell_amount": 0.5,
                "net_amount": 2.5,
            },
        ])
    if api_name == "moneyflow_ind_dc":
        trade_date = (params or {}).get("trade_date", "20260508")
        return pd.DataFrame([
            {
                "trade_date": trade_date,
                "content_type": "行业",
                "ts_code": "BK0475",
                "name": "银行",
                "pct_change": 1.1,
                "close": 1000.0,
                "net_amount": 100000000.0,
                "net_amount_rate": 2.0,
                "buy_elg_amount": 50000000.0,
                "buy_elg_amount_rate": 1.0,
                "buy_lg_amount": 50000000.0,
                "buy_lg_amount_rate": 1.0,
                "buy_md_amount": -10000000.0,
                "buy_md_amount_rate": -0.2,
                "buy_sm_amount": -90000000.0,
                "buy_sm_amount_rate": -1.8,
                "buy_sm_amount_stock": "浦发银行",
                "rank": 2,
            },
            {
                "trade_date": trade_date,
                "content_type": "行业",
                "ts_code": "BK0477",
                "name": "白酒",
                "pct_change": 2.2,
                "close": 2000.0,
                "net_amount": 300000000.0,
                "net_amount_rate": 5.0,
                "buy_elg_amount": 200000000.0,
                "buy_elg_amount_rate": 3.0,
                "buy_lg_amount": 100000000.0,
                "buy_lg_amount_rate": 2.0,
                "buy_md_amount": -50000000.0,
                "buy_md_amount_rate": -1.0,
                "buy_sm_amount": -250000000.0,
                "buy_sm_amount_rate": -4.0,
                "buy_sm_amount_stock": "贵州茅台",
                "rank": 1,
            },
        ])
    if api_name == "moneyflow_cnt_ths":
        trade_date = (params or {}).get("trade_date", "20260508")
        return pd.DataFrame([
            {
                "trade_date": trade_date,
                "ts_code": "885001.TI",
                "name": "人工智能",
                "lead_stock": "科大讯飞",
                "close_price": 42.0,
                "pct_change": 3.5,
                "industry_index": 1200.0,
                "company_num": 80,
                "pct_change_stock": 10.0,
                "net_buy_amount": 4.0,
                "net_sell_amount": 1.0,
                "net_amount": 3.0,
            },
        ])
    if api_name == "ths_member":
        return pd.DataFrame([
            {
                "ts_code": (params or {}).get("ts_code", "885001.TI"),
                "con_code": "600519.SH",
                "con_name": "贵州茅台",
                "weight": 5.0,
                "in_date": "20200101",
                "out_date": None,
                "is_new": "Y",
            },
            {
                "ts_code": (params or {}).get("ts_code", "885001.TI"),
                "con_code": "600000.SH",
                "con_name": "浦发银行",
                "weight": 3.0,
                "in_date": "20200101",
                "out_date": None,
                "is_new": "N",
            },
        ])
    if api_name == "top_list":
        trade_date = (params or {}).get("trade_date", "20260508")
        return pd.DataFrame([
            {
                "trade_date": trade_date,
                "ts_code": "600000.SH",
                "name": "浦发银行",
                "close": 8.12,
                "pct_change": -1.0,
                "turnover_rate": 1.2,
                "amount": 100000000.0,
                "l_sell": 6000000.0,
                "l_buy": 7000000.0,
                "l_amount": 13000000.0,
                "net_amount": 1000000.0,
                "net_rate": 1.0,
                "amount_rate": 13.0,
                "reason": "日涨幅偏离值达7%",
            },
            {
                "trade_date": trade_date,
                "ts_code": "600519.SH",
                "name": "贵州茅台",
                "close": 1688.0,
                "pct_change": 3.0,
                "turnover_rate": 2.0,
                "amount": 200000000.0,
                "l_sell": 20000000.0,
                "l_buy": 50000000.0,
                "l_amount": 70000000.0,
                "net_amount": 30000000.0,
                "net_rate": 15.0,
                "amount_rate": 35.0,
                "reason": "机构买入",
            },
        ])
    if api_name == "top_inst":
        trade_date = (params or {}).get("trade_date", "20260508")
        return pd.DataFrame([
            {
                "trade_date": trade_date,
                "ts_code": "600519.SH",
                "exalter": "机构专用",
                "side": "1",
                "buy": 50000000.0,
                "buy_rate": 20.0,
                "sell": 10000000.0,
                "sell_rate": 4.0,
                "net_buy": 40000000.0,
                "reason": "机构买入",
            },
            {
                "trade_date": trade_date,
                "ts_code": "600519.SH",
                "exalter": "某营业部",
                "side": "1",
                "buy": 10000000.0,
                "buy_rate": 5.0,
                "sell": 2000000.0,
                "sell_rate": 1.0,
                "net_buy": 8000000.0,
                "reason": "机构买入",
            },
        ])
    if api_name == "limit_list_ths":
        trade_date = (params or {}).get("trade_date", "20260508")
        return pd.DataFrame([
            {
                "trade_date": trade_date,
                "ts_code": "600000.SH",
                "name": "浦发银行",
                "price": 8.12,
                "pct_chg": 10.0,
                "open_num": 2,
                "lu_desc": "银行",
                "limit_type": "涨停池",
                "tag": "首板",
                "status": "换手板",
                "limit_order": 1000000.0,
                "limit_amount": 8000000.0,
                "turnover_rate": 3.0,
                "turnover": 100000000.0,
            },
            {
                "trade_date": trade_date,
                "ts_code": "600519.SH",
                "name": "贵州茅台",
                "price": 1688.0,
                "pct_chg": 10.0,
                "open_num": 0,
                "lu_desc": "白酒",
                "limit_type": "涨停池",
                "tag": "3天2板",
                "status": "一字板",
                "limit_order": 2000000.0,
                "limit_amount": 300000000.0,
                "turnover_rate": 1.0,
                "turnover": 200000000.0,
            },
        ])
    if api_name == "limit_list_d":
        trade_date = (params or {}).get("trade_date", "20260508")
        return pd.DataFrame([
            {
                "trade_date": trade_date,
                "ts_code": "600000.SH",
                "industry": "银行",
                "name": "浦发银行",
                "close": 8.12,
                "pct_chg": 10.0,
                "amount": 100000000.0,
                "fd_amount": 8000000.0,
                "first_time": "09:35:00",
                "last_time": "14:30:00",
                "open_times": 2,
                "up_stat": "1/1",
                "limit_times": 1,
                "limit": "U",
            },
            {
                "trade_date": trade_date,
                "ts_code": "600519.SH",
                "industry": "白酒",
                "name": "贵州茅台",
                "close": 1688.0,
                "pct_chg": 10.0,
                "amount": 200000000.0,
                "fd_amount": 300000000.0,
                "first_time": "09:30:00",
                "last_time": "09:30:00",
                "open_times": 0,
                "up_stat": "2/2",
                "limit_times": 2,
                "limit": "U",
            },
        ])
    if api_name == "limit_step":
        trade_date = (params or {}).get("trade_date", "20260508")
        return pd.DataFrame([
            {"trade_date": trade_date, "ts_code": "600000.SH", "name": "浦发银行", "nums": "1"},
            {"trade_date": trade_date, "ts_code": "600519.SH", "name": "贵州茅台", "nums": "3"},
        ])
    if api_name in {"ths_hot", "dc_hot"}:
        trade_date = (params or {}).get("trade_date", "20260508")
        data_type = "热股" if api_name == "ths_hot" else "A股市场"
        return pd.DataFrame([
            {
                "trade_date": trade_date,
                "data_type": data_type,
                "ts_code": "600000.SH",
                "ts_name": "浦发银行",
                "rank": 3,
                "pct_change": -1.0,
                "current_price": 8.12,
                "concept": '["银行"]',
                "rank_reason": "关注提升",
                "hot": 1000000.0,
                "rank_time": "2026-05-08 10:00:00",
            },
            {
                "trade_date": trade_date,
                "data_type": data_type,
                "ts_code": "600519.SH",
                "ts_name": "贵州茅台",
                "rank": 1,
                "pct_change": 3.0,
                "current_price": 1688.0,
                "concept": '["白酒"]',
                "rank_reason": "热度第一",
                "hot": 5000000.0,
                "rank_time": "2026-05-08 10:00:00",
            },
            {
                "trade_date": trade_date,
                "data_type": data_type,
                "ts_code": "600519.SH",
                "ts_name": "贵州茅台",
                "rank": 2,
                "pct_change": 2.0,
                "current_price": 1600.0,
                "concept": '["白酒"]',
                "rank_reason": "旧切片",
                "hot": 3000000.0,
                "rank_time": "2026-05-08 09:30:00",
            },
        ])
    if api_name in {"daily", "weekly", "monthly"}:
        return pd.DataFrame([{"ts_code": "603418.SH", "trade_date": "20260514", "close": 34.5}])
    if api_name in {"income", "balancesheet", "cashflow"}:
        return pd.DataFrame([{"ts_code": "603418.SH", "end_date": "20251231"}])
    if api_name == "anns_d":
        return pd.DataFrame([{"ann_date": "20260508", "ts_code": "600519.SH", "name": "贵州茅台", "title": "年度权益分派公告", "url": "http://unit/ann"}])
    if api_name == "stk_alert":
        return pd.DataFrame([{"ts_code": "600519.SH", "name": "贵州茅台", "start_date": "20260508", "end_date": "20260509", "type": "重点监控"}])
    if api_name in {"stk_shock", "stk_high_shock"}:
        return pd.DataFrame([{"ts_code": "600519.SH", "trade_date": "20260508", "name": "贵州茅台", "trade_market": "沪市", "reason": "异常波动", "period": "3日"}])
    if api_name == "pledge_detail":
        return pd.DataFrame([{"ts_code": "600519.SH", "ann_date": "20260508", "holder_name": "股东A", "pledge_amount": 100.0}])
    if api_name == "pledge_stat":
        return pd.DataFrame([{"ts_code": "600519.SH", "end_date": "20260508", "pledge_count": 2, "pledge_ratio": 1.5}])
    if api_name == "share_float":
        return pd.DataFrame([{"ts_code": "600519.SH", "ann_date": "20260508", "float_date": "20260601", "float_share": 100.0, "float_ratio": 1.0}])
    if api_name == "stk_holdertrade":
        return pd.DataFrame([{"ts_code": "600519.SH", "ann_date": "20260508", "holder_name": "股东A", "in_de": "DE", "change_vol": -100.0}])
    if api_name == "repurchase":
        return pd.DataFrame([{"ts_code": "600519.SH", "ann_date": "20260508", "end_date": "20260601", "proc": "实施", "amount": 100.0}])
    if api_name == "daily_basic":
        return pd.DataFrame([{"ts_code": "600519.SH", "trade_date": "20260508", "close": 1688.0, "pe_ttm": 28.0, "pb": 9.0, "turnover_rate": 1.2, "total_mv": 20000000.0}])
    if api_name == "fina_indicator":
        return pd.DataFrame([{"ts_code": "600519.SH", "end_date": "20251231", "roe": 30.0, "grossprofit_margin": 90.0, "debt_to_assets": 20.0}])
    if api_name == "forecast":
        return pd.DataFrame([{"ts_code": "600519.SH", "ann_date": "20260508", "end_date": "20260630", "type": "预增", "p_change_min": 20.0, "summary": "业绩预增"}])
    if api_name == "express":
        return pd.DataFrame([{"ts_code": "600519.SH", "ann_date": "20260508", "end_date": "20251231", "revenue": 100.0, "yoy_net_profit": 12.0}])
    if api_name == "dividend":
        return pd.DataFrame([{"ts_code": "600519.SH", "ann_date": "20260508", "cash_div": 30.0, "record_date": "20260601", "ex_date": "20260602"}])
    if api_name == "adj_factor":
        return pd.DataFrame([{"ts_code": "600519.SH", "trade_date": "20260508", "adj_factor": 12.34}])
    if api_name == "stk_factor":
        return pd.DataFrame([{
            "ts_code": (params or {}).get("ts_code", "600519.SH"),
            "trade_date": "20260508",
            "close": 1688.0,
            "open": 1660.0,
            "high": 1699.0,
            "low": 1650.0,
            "pre_close": 1658.0,
            "change": 30.0,
            "pct_change": 1.81,
            "vol": 10000.0,
            "amount": 1688000.0,
            "adj_factor": 12.34,
            "open_hfq": 20000.0,
            "open_qfq": 1660.0,
            "close_hfq": 20200.0,
            "close_qfq": 1688.0,
            "high_hfq": 20300.0,
            "high_qfq": 1699.0,
            "low_hfq": 19800.0,
            "low_qfq": 1650.0,
            "pre_close_hfq": 19900.0,
            "pre_close_qfq": 1658.0,
            "macd_dif": 1.2,
            "macd_dea": 0.8,
            "macd": 0.4,
            "kdj_k": 72.0,
            "kdj_d": 64.0,
            "kdj_j": 88.0,
            "rsi_6": 61.0,
            "rsi_12": 58.0,
            "rsi_24": 55.0,
            "boll_upper": 1710.0,
            "boll_mid": 1600.0,
            "boll_lower": 1490.0,
            "cci": 120.0,
        }])
    if api_name == "index_daily":
        return pd.DataFrame([{"ts_code": "000300.SH", "trade_date": "20260508", "close": 4000.0, "pct_chg": 1.0}])
    if api_name == "margin":
        return pd.DataFrame([{"trade_date": "20260514", "exchange_id": (params or {}).get("exchange_id"), "rzye": 100.0}])
    if api_name == "moneyflow_hsgt":
        return pd.DataFrame([{"trade_date": "20260508", "north_money": 12.5, "south_money": 3.2, "hgt": 6.0, "sgt": 6.5}])
    if api_name == "moneyflow_mkt_dc":
        return pd.DataFrame([{"trade_date": "20260508", "close_sh": 3100.0, "pct_change_sh": 1.2, "close_sz": 9800.0, "pct_change_sz": 1.8, "net_amount": 200000000.0, "net_amount_rate": 2.5, "buy_elg_amount": 100000000.0, "buy_elg_amount_rate": 1.2, "buy_lg_amount": 100000000.0, "buy_lg_amount_rate": 1.3}])
    if api_name == "hsgt_top10":
        return pd.DataFrame([
            {"trade_date": "20260508", "ts_code": "600519.SH", "name": "贵州茅台", "rank": 1, "market_type": (params or {}).get("market_type", "1"), "amount": 50.0, "net_amount": 20.0, "buy": 35.0, "sell": 15.0},
            {"trade_date": "20260508", "ts_code": "600000.SH", "name": "浦发银行", "rank": 2, "market_type": (params or {}).get("market_type", "1"), "amount": 30.0, "net_amount": -10.0, "buy": 10.0, "sell": 20.0},
        ])
    if api_name == "margin_detail":
        return pd.DataFrame([
            {"trade_date": "20260508", "ts_code": "600519.SH", "name": "贵州茅台", "rzye": 500000000.0, "rqye": 1000000.0, "rzmre": 80000000.0, "rqyl": 10.0, "rzche": 20000000.0, "rqchl": 2.0, "rzrqye": 501000000.0},
            {"trade_date": "20260508", "ts_code": "600000.SH", "name": "浦发银行", "rzye": 300000000.0, "rqye": 1000000.0, "rzmre": 40000000.0, "rqyl": 8.0, "rzche": 10000000.0, "rqchl": 1.0, "rzrqye": 301000000.0},
        ])
    if api_name == "block_trade":
        return pd.DataFrame([
            {"trade_date": "20260508", "ts_code": "600519.SH", "name": "贵州茅台", "price": 1680.0, "vol": 10.0, "amount": 1680.0, "buyer": "机构专用", "seller": "营业部A"},
            {"trade_date": "20260508", "ts_code": "600000.SH", "name": "浦发银行", "price": 8.0, "vol": 100.0, "amount": 80.0, "buyer": "营业部B", "seller": "营业部C"},
        ])
    return pd.DataFrame([{"ts_code": "603418.SH", "api_name": api_name}])


def test_get_tushare_basic_data_stock_normalizes_rows():
    with patch("data_provider.tushare_client.query_tushare_api", side_effect=_fake_query), \
            patch("data_provider.tushare_client.get_tushare_http_url", return_value="http://unit/"):
        result = _handle_get_tushare_basic_data(asset_type="stock", limit=5)

    assert result["status"] == "ok"
    assert result["api_name"] == "stock_basic"
    assert result["items"][0]["name"] == "友升股份"
    assert result["source_chain"][0]["endpoint"] == "http://unit/"


def test_get_tushare_daily_bars_converts_code_and_period():
    seen = {}

    def capture(api_name, params=None, fields="", timeout=30):
        seen["api_name"] = api_name
        seen["params"] = params
        return _fake_query(api_name, params=params, fields=fields, timeout=timeout)

    with patch("data_provider.tushare_client.query_tushare_api", side_effect=capture), \
            patch("data_provider.tushare_client.get_tushare_http_url", return_value="http://unit/"):
        result = _handle_get_tushare_daily_bars("603418", period="weekly", start_date="2026-05-01", end_date="20260515")

    assert result["status"] == "ok"
    assert seen["api_name"] == "weekly"
    assert seen["params"]["ts_code"] == "603418.SH"
    assert seen["params"]["start_date"] == "20260501"


def test_get_tushare_financial_statements_returns_three_blocks():
    with patch("data_provider.tushare_client.query_tushare_api", side_effect=_fake_query), \
            patch("data_provider.tushare_client.get_tushare_http_url", return_value="http://unit/"):
        result = _handle_get_tushare_financial_statements("603418", period="20251231")

    assert result["status"] == "ok"
    assert set(result["blocks"]) == {"income", "balancesheet", "cashflow"}


def test_get_tushare_reference_events_can_select_unlock():
    seen = {}

    def capture(api_name, params=None, fields="", timeout=30):
        seen["api_name"] = api_name
        seen["params"] = params
        return _fake_query(api_name, params=params, fields=fields, timeout=timeout)

    with patch("data_provider.tushare_client.query_tushare_api", side_effect=capture), \
            patch("data_provider.tushare_client.get_tushare_http_url", return_value="http://unit/"):
        result = _handle_get_tushare_reference_events("603418", event_type="unlock", start_date="20250101")

    assert result["status"] == "ok"
    assert seen["api_name"] == "share_float"
    assert seen["params"]["ts_code"] == "603418.SH"
    assert "unlock" in result["blocks"]


def test_get_tushare_moneyflow_ths_uses_latest_trade_date_and_normalizes_rows():
    with patch("data_provider.tushare_client.query_tushare_api", side_effect=_fake_query), \
            patch("data_provider.tushare_client.get_tushare_http_url", return_value="http://unit/"):
        result = _handle_get_tushare_moneyflow_ths(limit=2)

    assert result["status"] == "ok"
    assert result["api_name"] == "moneyflow_ths"
    assert result["trade_date"] == "20260508"
    assert [item["code"] for item in result["items"]] == ["600519", "600000"]
    assert result["items"][0]["net_inflow"] == 200000.0
    assert result["items"][0]["net_5d_inflow"] == 500000.0
    assert result["items"][0]["large_net_inflow"] == 120000.0
    assert result["items"][0]["large_net_inflow_rate"] == 60.0
    assert result["items"][0]["source"] == "tushare:moneyflow_ths"
    assert any(step["provider"] == "tushare:moneyflow_ths" for step in result["source_chain"])


def test_get_tushare_moneyflow_ths_filters_stock_code_without_fallback():
    seen = []

    def capture(api_name, params=None, fields="", timeout=30):
        seen.append((api_name, dict(params or {}), fields))
        return _fake_query(api_name, params=params, fields=fields, timeout=timeout)

    with patch("data_provider.tushare_client.query_tushare_api", side_effect=capture), \
            patch("data_provider.tushare_client.get_tushare_http_url", return_value="http://unit/"):
        result = _handle_get_tushare_moneyflow_ths(trade_date="20260508", stock_code="600519.SH", limit=10)

    assert [call[0] for call in seen] == ["moneyflow_ths"]
    assert seen[0][1]["trade_date"] == "20260508"
    assert result["status"] == "ok"
    assert len(result["items"]) == 1
    assert result["items"][0]["code"] == "600519"
    assert result["items"][0]["ts_code"] == "600519.SH"
    assert result["items"][0]["net_inflow"] == 200000.0


def test_get_tushare_moneyflow_dc_normalizes_amounts():
    with patch("data_provider.tushare_client.query_tushare_api", side_effect=_fake_query), \
            patch("data_provider.tushare_client.get_tushare_http_url", return_value="http://unit/"):
        result = _handle_get_tushare_moneyflow_dc(limit=2)

    assert result["status"] == "ok"
    assert result["api_name"] == "moneyflow_dc"
    assert [item["code"] for item in result["items"]] == ["600519", "600000"]
    assert result["items"][0]["net_inflow"] == 300000.0
    assert result["items"][0]["extra_large_net_inflow"] == 200000.0
    assert result["items"][0]["source"] == "tushare:moneyflow_dc"


def test_get_tushare_sector_moneyflow_tools_normalize_board_rows():
    with patch("data_provider.tushare_client.query_tushare_api", side_effect=_fake_query), \
            patch("data_provider.tushare_client.get_tushare_http_url", return_value="http://unit/"):
        ths = _handle_get_tushare_moneyflow_ind_ths(limit=2)
        dc = _handle_get_tushare_moneyflow_ind_dc(limit=2)
        cnt = _handle_get_tushare_moneyflow_cnt_ths(limit=2)
        unified = _handle_get_board_capital_flow(limit=2)
        members = _handle_get_tushare_ths_member(ts_code="885001.TI", limit=2)

    assert ths["status"] == "ok"
    assert ths["api_name"] == "moneyflow_ind_ths"
    assert ths["items"][0]["ts_code"] == "881002.TI"
    assert ths["items"][0]["net_inflow"] == 250000000.0
    assert dc["items"][0]["ts_code"] == "BK0477"
    assert dc["items"][0]["net_inflow"] == 300000000.0
    assert cnt["api_name"] == "moneyflow_cnt_ths"
    assert cnt["items"][0]["name"] == "人工智能"
    assert cnt["items"][0]["net_inflow"] == 300000000.0
    assert unified["api_name"] == "board_capital_flow"
    assert unified["status"] == "ok"
    assert unified["selected_flow_source"] == "tushare_moneyflow_ind_dc"
    assert unified["amount_unit"] == "CNY"
    assert "tushare_moneyflow_ind_dc" in unified["flow_sources"]
    assert "tushare_moneyflow_ind_ths" in unified["flow_sources"]
    assert "tushare_moneyflow_cnt_ths" in unified["flow_sources"]
    assert unified["selected_top_boards"][0]["name"] == "白酒"
    assert unified["selected_top_boards"][0]["main_net_inflow"] == 300000000.0
    assert "DC, THS industry, and THS concept" in unified["notes"][0]
    assert members["api_name"] == "ths_member"
    assert [item["code"] for item in members["items"]] == ["600519", "600000"]


def test_get_tushare_moneyflow_ind_ths_accepts_date_range_params():
    seen = []

    def capture(api_name, params=None, fields="", timeout=30):
        seen.append((api_name, dict(params or {}), fields))
        return _fake_query(api_name, params=params, fields=fields, timeout=timeout)

    with patch("data_provider.tushare_client.query_tushare_api", side_effect=capture), \
            patch("data_provider.tushare_client.get_tushare_http_url", return_value="http://unit/"):
        result = _handle_get_tushare_moneyflow_ind_ths(
            ts_code="881273.TI",
            start_date="2024-09-01",
            end_date="2024-09-27",
            limit=5,
        )

    assert [call[0] for call in seen] == ["moneyflow_ind_ths"]
    assert seen[0][1]["ts_code"] == "881273.TI"
    assert seen[0][1]["start_date"] == "20240901"
    assert seen[0][1]["end_date"] == "20240927"
    assert result["api_name"] == "moneyflow_ind_ths"
    assert result["start_date"] == "20240901"
    assert result["end_date"] == "20240927"


def test_tushare_board_capital_flow_tools_are_registered():
    names = {tool.name for tool in ALL_DATA_TOOLS}
    assert "get_tushare_moneyflow_ind_ths" in names
    assert "get_tushare_moneyflow_ind_dc" in names
    assert "get_tushare_moneyflow_cnt_ths" in names
    assert "get_board_capital_flow" in names


def test_get_tushare_dragon_tiger_list_normalizes_rows():
    with patch("data_provider.tushare_client.query_tushare_api", side_effect=_fake_query), \
            patch("data_provider.tushare_client.get_tushare_http_url", return_value="http://unit/"):
        result = _handle_get_tushare_dragon_tiger_list(limit=2)

    assert result["status"] == "ok"
    assert result["api_name"] == "top_list"
    assert result["items"][0]["code"] == "600519"
    assert result["items"][0]["net_inflow"] == 30000000.0
    assert result["items"][0]["reason"] == "机构买入"


def test_get_tushare_dragon_tiger_inst_groups_by_stock():
    with patch("data_provider.tushare_client.query_tushare_api", side_effect=_fake_query), \
            patch("data_provider.tushare_client.get_tushare_http_url", return_value="http://unit/"):
        result = _handle_get_tushare_dragon_tiger_inst(limit=2)

    assert result["status"] == "ok"
    assert result["api_name"] == "top_inst"
    assert result["items"][0]["code"] == "600519"
    assert result["items"][0]["seat_count"] == 2
    assert result["items"][0]["institution_seat_count"] == 1
    assert result["items"][0]["net_inflow"] == 48000000.0
    assert len(result["items"][0]["top_seats"]) == 2


def test_get_tushare_limit_lists_normalize_streak_and_amounts():
    with patch("data_provider.tushare_client.query_tushare_api", side_effect=_fake_query), \
            patch("data_provider.tushare_client.get_tushare_http_url", return_value="http://unit/"):
        ths = _handle_get_tushare_limit_list_ths(limit=2)
        daily = _handle_get_tushare_limit_list_d(limit=2)
        step = _handle_get_tushare_limit_step(limit=2)

    assert ths["status"] == "ok"
    assert ths["items"][0]["code"] == "600519"
    assert ths["items"][0]["limit_up_streak"] == 2.0
    assert ths["items"][0]["ceiling_amount"] == 300000000.0
    assert daily["items"][0]["code"] == "600519"
    assert daily["items"][0]["limit_up_streak"] == 2.0
    assert daily["items"][0]["first_limit_time"] == "09:30:00"
    assert step["items"][0]["code"] == "600519"
    assert step["items"][0]["limit_up_streak"] == 3.0


def test_get_tushare_hot_rank_keeps_best_rank_per_stock():
    with patch("data_provider.tushare_client.query_tushare_api", side_effect=_fake_query), \
            patch("data_provider.tushare_client.get_tushare_http_url", return_value="http://unit/"):
        result = _handle_get_tushare_hot_rank(source="ths", limit=5)

    assert result["status"] == "ok"
    assert result["api_name"] == "ths_hot"
    assert result["rank_time"] == "2026-05-08 10:00:00"
    assert [item["code"] for item in result["items"]] == ["600519", "600000"]
    assert result["items"][0]["rank"] == 1.0
    assert result["items"][0]["rank_time"] == "2026-05-08 10:00:00"
    assert result["items"][0]["concepts"] == ["白酒"]


def test_get_tushare_event_fundamental_and_technical_tools_query_expected_apis():
    with patch("data_provider.tushare_client.query_tushare_api", side_effect=_fake_query), \
            patch("data_provider.tushare_client.get_tushare_http_url", return_value="http://unit/"):
        announcements = _handle_get_tushare_announcements(stock_code="600519", limit=1)
        alerts = _handle_get_tushare_stock_alerts(stock_code="600519", limit=1)
        shock = _handle_get_tushare_stock_shock(stock_code="600519", limit=1)
        pledge_stat = _handle_get_tushare_pledge_stat(stock_code="600519", limit=1)
        pledge = _handle_get_tushare_pledge_detail(stock_code="600519", limit=1)
        unlock = _handle_get_tushare_share_float(stock_code="600519", limit=1)
        holder_trade = _handle_get_tushare_holder_trade(stock_code="600519", limit=1)
        repurchase = _handle_get_tushare_repurchase(stock_code="600519", limit=1)
        daily_basic = _handle_get_tushare_daily_basic(stock_code="600519", trade_date="20260508", limit=1)
        daily_basic_default = _handle_get_tushare_daily_basic(limit=1)
        fina = _handle_get_tushare_financial_indicators(stock_code="600519", period="20251231", limit=1)
        forecast = _handle_get_tushare_forecast(stock_code="600519", period="20260630", limit=1)
        forecast_missing = _handle_get_tushare_forecast(limit=1)
        express = _handle_get_tushare_express(stock_code="600519", period="20251231", limit=1)
        express_missing = _handle_get_tushare_express(limit=1)
        dividend = _handle_get_tushare_dividend(stock_code="600519", limit=1)
        dividend_missing = _handle_get_tushare_dividend(limit=1)
        adj = _handle_get_tushare_adj_factor(stock_code="600519", trade_date="20260508", limit=1)
        factors = _handle_get_tushare_stk_factor(stock_code="600519", start_date="20260501", end_date="20260508", limit=5)
        index_daily = _handle_get_tushare_index_daily(index_code="000300", trade_date="20260508", limit=1)
        index_daily_alias = _handle_get_tushare_index_daily(ts_code="000300.SH", trade_date="20260508", limit=1)
        trade_cal = _handle_get_tushare_trade_calendar(start_date="20260501", end_date="20260508", limit=2)

    assert announcements["api_name"] == "anns_d"
    assert announcements["items"][0]["title"] == "年度权益分派公告"
    assert announcements["date_window"]["start_date"]
    assert announcements["date_window"]["end_date"]
    assert alerts["api_name"] == "stk_alert"
    assert shock["api_name"] == "stk_shock"
    assert pledge_stat["api_name"] == "pledge_stat"
    assert pledge["api_name"] == "pledge_detail"
    assert unlock["api_name"] == "share_float"
    assert holder_trade["api_name"] == "stk_holdertrade"
    assert repurchase["api_name"] == "repurchase"
    assert daily_basic["api_name"] == "daily_basic"
    assert daily_basic["items"][0]["pe_ttm"] == 28.0
    assert daily_basic_default["api_name"] == "daily_basic"
    assert fina["api_name"] == "fina_indicator"
    assert forecast["api_name"] == "forecast"
    assert forecast_missing["status"] == "failed"
    assert express["api_name"] == "express"
    assert express_missing["status"] == "failed"
    assert dividend["api_name"] == "dividend"
    assert dividend_missing["status"] == "failed"
    assert adj["api_name"] == "adj_factor"
    assert factors["api_name"] == "stk_factor"
    assert factors["stock_code"] == "600519"
    assert factors["ts_code"] == "600519.SH"
    assert factors["latest"]["macd"] == 0.4
    assert factors["latest"]["kdj_j"] == 88.0
    assert factors["latest"]["rsi_6"] == 61.0
    assert factors["latest"]["boll_upper"] == 1710.0
    assert "front-adjusted prices" in factors["notes"][0]
    assert index_daily["api_name"] == "index_daily"
    assert index_daily_alias["index_code"] == "000300.SH"
    assert trade_cal["api_name"] == "trade_cal"


def test_get_tushare_today_news_uses_current_day_window_and_is_registered():
    calls = []

    def fake_news_query(api_name, params=None, fields="", timeout=30):
        calls.append({"api_name": api_name, "params": params or {}, "fields": fields, "timeout": timeout})
        return pd.DataFrame([
            {
                "datetime": "2026-05-31 09:31:00",
                "title": "A股早盘快讯",
                "content": "今日市场快讯内容" * 80,
                "channels": "7*24",
            },
            {
                "datetime": "2026-05-31 09:32:00",
                "title": "第二条快讯",
                "content": "第二条内容",
                "channels": "财经",
            },
        ])

    fixed_now = datetime(2026, 5, 31, 17, 58, 30)
    with patch("src.agent.tools.data_tools.datetime") as mock_datetime, \
            patch("data_provider.tushare_client.query_tushare_api", side_effect=fake_news_query), \
            patch("data_provider.tushare_client.get_tushare_http_url", return_value="http://unit/"):
        mock_datetime.now.return_value = fixed_now
        result = _handle_get_tushare_today_news(src="sina", limit=1)

    assert result["status"] == "ok"
    assert result["api_name"] == "news"
    assert result["src"] == "sina"
    assert result["start_date"] == "2026-05-31 00:00:00"
    assert result["end_date"] == "2026-05-31 17:58:30"
    assert result["limit"] == 1
    assert len(result["items"]) == 1
    assert len(result["items"][0]["content"]) < 530
    assert calls[0]["api_name"] == "news"
    assert calls[0]["params"] == {
        "src": "sina",
        "start_date": "2026-05-31 00:00:00",
        "end_date": "2026-05-31 17:58:30",
    }
    assert calls[0]["fields"] == "datetime,content,title,channels"
    assert "get_tushare_today_news" in {tool.name for tool in ALL_DATA_TOOLS}


def test_get_tushare_today_news_rejects_unsupported_source_without_query():
    with patch("data_provider.tushare_client.query_tushare_api") as query:
        result = _handle_get_tushare_today_news(src="bad_source", limit=1)

    assert result["status"] == "failed"
    assert result["api_name"] == "news"
    assert result["items"] == []
    assert "unsupported Tushare news src" in result["errors"][0]
    query.assert_not_called()


def test_get_margin_trading_summary_uses_tushare_margin():
    with patch("data_provider.tushare_client.query_tushare_api", side_effect=_fake_query), \
            patch("data_provider.tushare_client.get_tushare_http_url", return_value="http://unit/"):
        result = _handle_get_margin_trading_summary(limit=2)

    assert result["status"] == "ok"
    assert result["sse"][0]["exchange_id"] == "SSE"
    assert result["szse"][0]["exchange_id"] == "SZSE"
    assert result["source_chain"][0]["provider"] == "tushare:margin"


def test_get_tushare_stock_connect_margin_and_block_tools_normalize_rows():
    with patch("data_provider.tushare_client.query_tushare_api", side_effect=_fake_query), \
            patch("data_provider.tushare_client.get_tushare_http_url", return_value="http://unit/"):
        hsgt_flow = _handle_get_tushare_moneyflow_hsgt(trade_date="20260508", limit=2)
        market_flow = _handle_get_tushare_moneyflow_mkt_dc(trade_date="20260508", limit=2)
        hsgt_top = _handle_get_tushare_hsgt_top10(trade_date="20260508", market_type="1", limit=2)
        margin = _handle_get_tushare_margin_detail(trade_date="20260508", limit=2)
        block = _handle_get_tushare_block_trade(trade_date="20260508", limit=2)

    assert hsgt_flow["api_name"] == "moneyflow_hsgt"
    assert hsgt_flow["items"][0]["north_money"] == 12.5
    assert market_flow["api_name"] == "moneyflow_mkt_dc"
    assert market_flow["items"][0]["net_amount"] == 200000000.0
    assert hsgt_top["api_name"] == "hsgt_top10"
    assert hsgt_top["items"][0]["code"] == "600519"
    assert hsgt_top["items"][0]["net_amount"] == 200000.0
    assert margin["api_name"] == "margin_detail"
    assert margin["items"][0]["code"] == "600519"
    assert margin["items"][0]["financing_buy"] == 80000000.0
    assert block["api_name"] == "block_trade"
    assert block["items"][0]["code"] == "600519"
    assert block["items"][0]["amount"] == 16800000.0


def test_get_tushare_moneyflow_mkt_dc_accepts_date_range_params():
    seen = []

    def capture(api_name, params=None, fields="", timeout=30):
        seen.append((api_name, dict(params or {}), fields))
        return _fake_query(api_name, params=params, fields=fields, timeout=timeout)

    with patch("data_provider.tushare_client.query_tushare_api", side_effect=capture), \
            patch("data_provider.tushare_client.get_tushare_http_url", return_value="http://unit/"):
        result = _handle_get_tushare_moneyflow_mkt_dc(
            start_date="2024-09-01",
            end_date="2024-09-30",
            limit=10,
        )

    assert [call[0] for call in seen] == ["moneyflow_mkt_dc"]
    assert seen[0][1]["start_date"] == "20240901"
    assert seen[0][1]["end_date"] == "20240930"
    assert result["api_name"] == "moneyflow_mkt_dc"
    assert result["start_date"] == "20240901"
    assert result["end_date"] == "20240930"
    assert result["items"][0]["net_amount"] == 200000000.0
