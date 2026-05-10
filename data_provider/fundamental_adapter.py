# -*- coding: utf-8 -*-
"""
AkShare fundamental adapter (fail-open).

This adapter intentionally uses capability probing against multiple AkShare
endpoint candidates. It should never raise to caller; partial data is allowed.
"""

from __future__ import annotations

import logging
import os
import re
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd
import requests

logger = logging.getLogger(__name__)

_DIVIDEND_KEYWORD_MAP: Dict[str, List[str]] = {
    "per_share": [
        "每股派息",
        "每股现金红利",
        "每股分红",
        "每股派现",
        "派现(元/股)",
        "派息(元/股)",
        "税前派息(元/股)",
        "现金分红(税前)",
    ],
    "plan_text": [
        "分配方案",
        "分红方案",
        "实施方案",
        "派息方案",
        "方案",
        "预案",
        "方案说明",
    ],
    "ex_dividend_date": ["除权除息日", "除息日", "除权日", "除权除息", "除息日期"],
    "record_date": ["股权登记日", "登记日"],
    "announce_date": ["公告日期", "公告日", "实施公告日", "预案公告日"],
    "report_date": ["报告期", "报告日期", "截止日期", "统计截止日期"],
}


def _safe_float(value: Any) -> Optional[float]:
    """Best-effort float conversion."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        try:
            return float(value)
        except (TypeError, ValueError):
            return None
    s = str(value).strip().replace(",", "").replace("%", "")
    if not s:
        return None
    try:
        return float(s)
    except (TypeError, ValueError):
        return None


def _safe_str(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _safe_datetime(value: Any) -> Optional[datetime]:
    if value is None:
        return None
    try:
        parsed = pd.to_datetime(value)
    except Exception:
        return None
    if pd.isna(parsed):
        return None
    try:
        return parsed.to_pydatetime()
    except Exception:
        return None


def _normalize_code(raw: Any) -> str:
    s = _safe_str(raw).upper()
    if "." in s:
        s = s.split(".", 1)[0]
    s = re.sub(r"^(SH|SZ|BJ)", "", s)
    return s


def _akshare_fund_flow_market(stock_code: str) -> str:
    """Return AkShare market parameter for A-share fund-flow endpoints."""
    code = _normalize_code(stock_code)
    if code.startswith(("60", "68", "900")):
        return "sh"
    if code.startswith(("43", "81", "82", "83", "87", "88", "92")):
        return "bj"
    return "sz"


def _stockapi_code_flow_url() -> str:
    return "https://www.stockapi.com.cn/v1/base/codeFlow"


def _pick_by_keywords(row: pd.Series, keywords: List[str]) -> Optional[Any]:
    """
    Return first non-empty row value whose column name contains any keyword.
    """
    for col in row.index:
        col_s = str(col)
        if any(k in col_s for k in keywords):
            val = row.get(col)
            if val is not None and str(val).strip() not in ("", "-", "nan", "None"):
                return val
    return None


def _parse_dividend_plan_to_per_share(plan_text: str) -> Optional[float]:
    """Parse per-share cash dividend from Chinese plan text."""
    text = _safe_str(plan_text)
    if not text:
        return None

    for pattern in (
        r"(?:每)?\s*10\s*股?\s*派(?:发)?\s*([0-9]+(?:\.[0-9]+)?)\s*元",
        r"10\s*派\s*([0-9]+(?:\.[0-9]+)?)\s*元",
    ):
        match = re.search(pattern, text)
        if match:
            parsed = _safe_float(match.group(1))
            if parsed is not None and parsed > 0:
                return parsed / 10.0

    match_per_share = re.search(r"每\s*股\s*派(?:发)?\s*([0-9]+(?:\.[0-9]+)?)\s*元", text)
    if match_per_share:
        parsed = _safe_float(match_per_share.group(1))
        if parsed is not None and parsed > 0:
            return parsed
    return None


def _extract_cash_dividend_per_share(row: pd.Series) -> Optional[float]:
    """Extract pre-tax cash dividend per share from a row."""
    plan_text = _safe_str(_pick_by_keywords(row, _DIVIDEND_KEYWORD_MAP["plan_text"]))
    # Keep pre-tax semantics; skip explicit after-tax plans unless pre-tax marker exists.
    if "税后" in plan_text and "税前" not in plan_text and "含税" not in plan_text:
        return None

    direct = _safe_float(_pick_by_keywords(row, _DIVIDEND_KEYWORD_MAP["per_share"]))
    if direct is not None and direct > 0:
        return direct
    return _parse_dividend_plan_to_per_share(plan_text)


def _filter_rows_by_code(df: pd.DataFrame, stock_code: str) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()
    code_cols = [c for c in df.columns if any(k in str(c) for k in ("代码", "股票代码", "证券代码", "symbol", "ts_code"))]
    if not code_cols:
        return df

    target = _normalize_code(stock_code)
    for col in code_cols:
        try:
            series = df[col].astype(str).map(_normalize_code)
            filtered = df[series == target]
            if not filtered.empty:
                return filtered
        except Exception:
            continue
    return pd.DataFrame()


def _normalize_report_date(value: Any) -> Optional[str]:
    parsed = _safe_datetime(value)
    return parsed.date().isoformat() if parsed else None


def _build_dividend_payload(
    dividend_df: pd.DataFrame,
    stock_code: str,
    max_events: int = 5,
) -> Dict[str, Any]:
    work_df = _filter_rows_by_code(dividend_df, stock_code)
    if work_df.empty:
        return {}

    now_date = datetime.now().date()
    ttm_start_date = now_date - timedelta(days=365)
    dedupe_keys = set()
    events: List[Dict[str, Any]] = []

    for _, row in work_df.iterrows():
        if not isinstance(row, pd.Series):
            continue
        ex_dt = _safe_datetime(_pick_by_keywords(row, _DIVIDEND_KEYWORD_MAP["ex_dividend_date"]))
        record_dt = _safe_datetime(_pick_by_keywords(row, _DIVIDEND_KEYWORD_MAP["record_date"]))
        announce_dt = _safe_datetime(_pick_by_keywords(row, _DIVIDEND_KEYWORD_MAP["announce_date"]))
        event_dt = ex_dt or record_dt or announce_dt
        if event_dt is None:
            continue
        event_date = event_dt.date()
        if event_date > now_date:
            continue

        per_share = _extract_cash_dividend_per_share(row)
        if per_share is None or per_share <= 0:
            continue

        dedupe_key = (event_date.isoformat(), round(per_share, 6))
        if dedupe_key in dedupe_keys:
            continue
        dedupe_keys.add(dedupe_key)

        events.append(
            {
                "event_date": event_date.isoformat(),
                "ex_dividend_date": ex_dt.date().isoformat() if ex_dt else None,
                "record_date": record_dt.date().isoformat() if record_dt else None,
                "announcement_date": announce_dt.date().isoformat() if announce_dt else None,
                "cash_dividend_per_share": round(per_share, 6),
                "is_pre_tax": True,
            }
        )

    if not events:
        return {}

    events.sort(key=lambda item: item.get("event_date") or "", reverse=True)
    ttm_events: List[Dict[str, Any]] = []
    for item in events:
        event_dt = _safe_datetime(item.get("event_date"))
        if event_dt is None:
            continue
        event_date = event_dt.date()
        if ttm_start_date <= event_date <= now_date:
            ttm_events.append(item)

    return {
        "events": events[:max(1, max_events)],
        "ttm_event_count": len(ttm_events),
        "ttm_cash_dividend_per_share": (
            round(sum(float(item.get("cash_dividend_per_share") or 0.0) for item in ttm_events), 6)
            if ttm_events else None
        ),
        "coverage": "cash_dividend_pre_tax",
        "as_of": now_date.isoformat(),
    }


def _extract_latest_row(df: pd.DataFrame, stock_code: str) -> Optional[pd.Series]:
    """
    Select the most relevant row for the given stock.
    """
    if df is None or df.empty:
        return None

    code_cols = [c for c in df.columns if any(k in str(c) for k in ("代码", "股票代码", "证券代码", "ts_code", "symbol"))]
    target = _normalize_code(stock_code)
    if code_cols:
        for col in code_cols:
            try:
                series = df[col].astype(str).map(_normalize_code)
                matched = df[series == target]
                if not matched.empty:
                    return matched.iloc[0]
            except Exception:
                continue
        return None

    # Fallback: use latest row
    return df.iloc[0]


def _first_column_by_keywords(df: pd.DataFrame, keywords: List[str]) -> Optional[str]:
    if df is None or df.empty:
        return None
    for col in df.columns:
        col_s = str(col)
        if any(keyword in col_s for keyword in keywords):
            return col
    return None


def _row_date_value(row: pd.Series) -> Optional[str]:
    value = _pick_by_keywords(row, ["日期", "交易日", "时间", "date"])
    parsed = _safe_datetime(value)
    return parsed.date().isoformat() if parsed else (_safe_str(value) or None)


def _compact_numeric_row(row: pd.Series, field_map: Dict[str, List[str]]) -> Dict[str, Any]:
    payload: Dict[str, Any] = {}
    for target_key, keywords in field_map.items():
        payload[target_key] = _safe_float(_pick_by_keywords(row, keywords))
    return payload


class AkshareFundamentalAdapter:
    """AkShare adapter for fundamentals, capital flow and dragon-tiger signals."""

    def _call_df_candidates(
        self,
        candidates: List[Tuple[str, Dict[str, Any]]],
        stop_error_keywords: Optional[List[str]] = None,
    ) -> Tuple[Optional[pd.DataFrame], Optional[str], List[str]]:
        errors: List[str] = []
        try:
            import akshare as ak
        except Exception as exc:
            return None, None, [f"import_akshare:{type(exc).__name__}"]

        for func_name, kwargs in candidates:
            fn = getattr(ak, func_name, None)
            if fn is None:
                continue
            try:
                df = fn(**kwargs)
                if isinstance(df, pd.Series):
                    df = df.to_frame().T
                if isinstance(df, pd.DataFrame) and not df.empty:
                    return df, func_name, errors
            except Exception as exc:
                message = str(exc).strip().replace("\n", " ")
                if len(message) > 220:
                    message = message[:217] + "..."
                error_text = f"{func_name}:{type(exc).__name__}:{message}"
                errors.append(error_text)
                if stop_error_keywords and any(keyword in error_text for keyword in stop_error_keywords):
                    break
                continue
        return None, None, errors

    def get_fundamental_bundle(self, stock_code: str) -> Dict[str, Any]:
        """
        Return normalized fundamental blocks from AkShare with partial tolerance.
        """
        result: Dict[str, Any] = {
            "status": "not_supported",
            "growth": {},
            "earnings": {},
            "institution": {},
            "source_chain": [],
            "errors": [],
        }

        # Financial indicators
        fin_df, fin_source, fin_errors = self._call_df_candidates([
            ("stock_financial_abstract", {"symbol": stock_code}),
            ("stock_financial_analysis_indicator", {"symbol": stock_code}),
            ("stock_financial_analysis_indicator", {}),
        ])
        result["errors"].extend(fin_errors)
        if fin_df is not None:
            row = _extract_latest_row(fin_df, stock_code)
            if row is not None:
                revenue_yoy = _safe_float(_pick_by_keywords(row, ["营业收入同比", "营收同比", "收入同比", "同比增长"]))
                profit_yoy = _safe_float(_pick_by_keywords(row, ["净利润同比", "净利同比", "归母净利润同比"]))
                roe = _safe_float(_pick_by_keywords(row, ["净资产收益率", "ROE", "净资产收益"]))
                gross_margin = _safe_float(_pick_by_keywords(row, ["毛利率"]))
                report_date = _normalize_report_date(_pick_by_keywords(row, _DIVIDEND_KEYWORD_MAP["report_date"]))
                revenue = _safe_float(_pick_by_keywords(row, ["营业总收入", "营业收入", "营收"]))
                net_profit_parent = _safe_float(_pick_by_keywords(row, ["归母净利润", "母公司股东净利润", "净利润"]))
                operating_cash_flow = _safe_float(
                    _pick_by_keywords(row, ["经营活动产生的现金流量净额", "经营现金流", "经营活动现金流"])
                )
                result["growth"] = {
                    "revenue_yoy": revenue_yoy,
                    "net_profit_yoy": profit_yoy,
                    "roe": roe,
                    "gross_margin": gross_margin,
                }
                financial_report_payload = {
                    "report_date": report_date,
                    "revenue": revenue,
                    "net_profit_parent": net_profit_parent,
                    "operating_cash_flow": operating_cash_flow,
                    "roe": roe,
                }
                if any(v is not None for v in financial_report_payload.values()):
                    result["earnings"]["financial_report"] = financial_report_payload
                result["source_chain"].append(f"growth:{fin_source}")

        # Earnings forecast
        forecast_df, forecast_source, forecast_errors = self._call_df_candidates([
            ("stock_yjyg_em", {"symbol": stock_code}),
            ("stock_yjyg_em", {}),
            ("stock_yjbb_em", {"symbol": stock_code}),
            ("stock_yjbb_em", {}),
        ])
        result["errors"].extend(forecast_errors)
        if forecast_df is not None:
            row = _extract_latest_row(forecast_df, stock_code)
            if row is not None:
                result["earnings"]["forecast_summary"] = _safe_str(
                    _pick_by_keywords(row, ["预告", "业绩变动", "内容", "摘要", "公告"])
                )[:200]
                result["source_chain"].append(f"earnings_forecast:{forecast_source}")

        # Earnings quick report
        quick_df, quick_source, quick_errors = self._call_df_candidates([
            ("stock_yjkb_em", {"symbol": stock_code}),
            ("stock_yjkb_em", {}),
        ])
        result["errors"].extend(quick_errors)
        if quick_df is not None:
            row = _extract_latest_row(quick_df, stock_code)
            if row is not None:
                result["earnings"]["quick_report_summary"] = _safe_str(
                    _pick_by_keywords(row, ["快报", "摘要", "公告", "说明"])
                )[:200]
                result["source_chain"].append(f"earnings_quick:{quick_source}")

        # Dividend details (cash dividend, pre-tax)
        dividend_df, dividend_source, dividend_errors = self._call_df_candidates([
            ("stock_fhps_detail_em", {"symbol": stock_code}),
            ("stock_history_dividend_detail", {"symbol": stock_code, "indicator": "分红", "date": ""}),
            ("stock_dividend_cninfo", {"symbol": stock_code}),
        ])
        result["errors"].extend(dividend_errors)
        if dividend_df is not None:
            dividend_payload = _build_dividend_payload(dividend_df, stock_code, max_events=5)
            if dividend_payload:
                result["earnings"]["dividend"] = dividend_payload
                result["source_chain"].append(f"dividend:{dividend_source}")

        # Institution / top shareholders
        inst_df, inst_source, inst_errors = self._call_df_candidates([
            ("stock_institute_hold", {}),
            ("stock_institute_recommend", {}),
        ])
        result["errors"].extend(inst_errors)
        if inst_df is not None:
            row = _extract_latest_row(inst_df, stock_code)
            if row is not None:
                inst_change = _safe_float(_pick_by_keywords(row, ["增减", "变化", "变动", "持股变化"]))
                result["institution"]["institution_holding_change"] = inst_change
                result["source_chain"].append(f"institution:{inst_source}")

        top10_df, top10_source, top10_errors = self._call_df_candidates([
            ("stock_gdfx_top_10_em", {"symbol": stock_code}),
            ("stock_gdfx_top_10_em", {}),
            ("stock_zh_a_gdhs_detail_em", {"symbol": stock_code}),
            ("stock_zh_a_gdhs_detail_em", {}),
        ])
        result["errors"].extend(top10_errors)
        if top10_df is not None:
            row = _extract_latest_row(top10_df, stock_code)
            if row is not None:
                holder_change = _safe_float(_pick_by_keywords(row, ["增减", "变化", "持股变化", "变动"]))
                result["institution"]["top10_holder_change"] = holder_change
                result["source_chain"].append(f"top10:{top10_source}")

        has_content = bool(result["growth"] or result["earnings"] or result["institution"])
        result["status"] = "partial" if has_content else "not_supported"
        return result

    def get_market_capital_flow(self, top_n: int = 5) -> Dict[str, Any]:
        """Return market/sector/industry capital flow snapshots."""
        effective_top_n = max(1, min(int(top_n or 5), 20))
        result: Dict[str, Any] = {
            "status": "not_supported",
            "market_flow": {},
            "individual_rankings": {"top": [], "bottom": []},
            "industry_rankings": {"top": [], "bottom": []},
            "concept_rankings": {"top": [], "bottom": []},
            "source_chain": [],
            "errors": [],
        }

        market_df, market_source, market_errors = self._call_df_candidates([
            ("stock_market_fund_flow", {}),
        ])
        result["errors"].extend(market_errors)
        if market_df is not None and not market_df.empty:
            row = market_df.iloc[0]
            result["market_flow"] = {
                "date": _row_date_value(row),
                **_compact_numeric_row(
                    row,
                    {
                        "main_net_inflow": ["主力净流入", "主力净额", "净流入", "净额"],
                        "super_large_net_inflow": ["超大单净流入", "超大单净额"],
                        "large_net_inflow": ["大单净流入", "大单净额"],
                        "medium_net_inflow": ["中单净流入", "中单净额"],
                        "small_net_inflow": ["小单净流入", "小单净额"],
                    },
                ),
            }
            result["source_chain"].append(f"market_flow:{market_source}")

        for key, candidates in {
            "individual_rankings": [
                ("stock_fund_flow_individual", {"symbol": "即时"}),
                ("stock_individual_fund_flow_rank", {"indicator": "今日"}),
                ("stock_individual_fund_flow_rank", {"indicator": "5日"}),
            ],
            "industry_rankings": [
                ("stock_fund_flow_industry", {"symbol": "即时"}),
                ("stock_sector_fund_flow_rank", {}),
                ("stock_sector_fund_flow_summary", {}),
            ],
            "concept_rankings": [
                ("stock_fund_flow_concept", {"symbol": "即时"}),
                ("stock_concept_fund_flow_hist", {}),
            ],
        }.items():
            df, source, errors = self._call_df_candidates(candidates)
            result["errors"].extend(errors)
            rankings = self._rank_flow_dataframe(df, effective_top_n)
            if rankings["top"] or rankings["bottom"]:
                result[key] = rankings
                result["source_chain"].append(f"{key}:{source}")

        has_content = bool(
            any(v is not None for v in result["market_flow"].values())
            or any(result[key]["top"] or result[key]["bottom"] for key in ("individual_rankings", "industry_rankings", "concept_rankings"))
        )
        result["status"] = "partial" if has_content else "not_supported"
        return result

    def get_northbound_capital_flow(self, limit: int = 10) -> Dict[str, Any]:
        """Return northbound / Stock Connect capital-flow summary."""
        effective_limit = max(1, min(int(limit or 10), 60))
        result: Dict[str, Any] = {
            "status": "not_supported",
            "summary": {},
            "history": [],
            "source_chain": [],
            "errors": [],
        }

        summary_df, summary_source, summary_errors = self._call_df_candidates([
            ("stock_hsgt_fund_flow_summary_em", {}),
        ])
        result["errors"].extend(summary_errors)
        if summary_df is not None and not summary_df.empty:
            row = summary_df.iloc[0]
            result["summary"] = {
                "date": _row_date_value(row),
                **_compact_numeric_row(
                    row,
                    {
                        "northbound_net_inflow": ["北向资金", "北上资金", "沪股通", "深股通", "净流入", "净买入"],
                        "southbound_net_inflow": ["南向资金", "港股通"],
                        "buy_amount": ["买入", "流入"],
                        "sell_amount": ["卖出", "流出"],
                    },
                ),
            }
            result["source_chain"].append(f"northbound_summary:{summary_source}")

        hist_df, hist_source, hist_errors = self._call_df_candidates([
            ("stock_hsgt_hist_em", {"symbol": "北向资金"}),
        ])
        result["errors"].extend(hist_errors)
        if hist_df is not None and not hist_df.empty:
            rows = hist_df.tail(effective_limit).to_dict(orient="records")
            result["history"] = [self._compact_northbound_history_row(row) for row in rows]
            result["source_chain"].append(f"northbound_history:{hist_source}")

        has_content = bool(result["summary"] or result["history"])
        result["status"] = "partial" if has_content else "not_supported"
        return result

    def get_margin_trading_summary(self, limit: int = 10) -> Dict[str, Any]:
        """Return margin financing/securities-lending summary for A-share markets."""
        effective_limit = max(1, min(int(limit or 10), 60))
        result: Dict[str, Any] = {
            "status": "not_supported",
            "account_info": {},
            "sse": [],
            "szse": [],
            "source_chain": [],
            "errors": [],
        }

        account_df, account_source, account_errors = self._call_df_candidates([
            ("stock_margin_account_info", {}),
        ])
        result["errors"].extend(account_errors)
        if account_df is not None and not account_df.empty:
            row = account_df.iloc[0]
            result["account_info"] = {
                "date": _row_date_value(row),
                **_compact_numeric_row(
                    row,
                    {
                        "financing_balance": ["融资余额"],
                        "securities_lending_balance": ["融券余额"],
                        "margin_balance": ["两融余额", "融资融券余额"],
                        "financing_buy_amount": ["融资买入额"],
                        "financing_repayment_amount": ["融资偿还额"],
                    },
                ),
            }
            result["source_chain"].append(f"margin_account:{account_source}")

        now = datetime.now()
        start_date = (now - timedelta(days=effective_limit * 3)).strftime("%Y%m%d")
        end_date = now.strftime("%Y%m%d")
        sse_df, sse_source, sse_errors = self._call_df_candidates([
            ("stock_margin_sse", {"start_date": start_date, "end_date": end_date}),
        ])
        result["errors"].extend(sse_errors)
        if sse_df is not None and not sse_df.empty:
            result["sse"] = [
                self._compact_margin_row(row)
                for row in sse_df.tail(effective_limit).to_dict(orient="records")
            ]
            result["source_chain"].append(f"margin_sse:{sse_source}")

        szse_df, szse_source, szse_errors = self._call_df_candidates([
            ("stock_margin_szse", {"date": end_date}),
        ])
        result["errors"].extend(szse_errors)
        if szse_df is not None and not szse_df.empty:
            result["szse"] = [
                self._compact_margin_row(row)
                for row in szse_df.tail(effective_limit).to_dict(orient="records")
            ]
            result["source_chain"].append(f"margin_szse:{szse_source}")

        has_content = bool(result["account_info"] or result["sse"] or result["szse"])
        result["status"] = "partial" if has_content else "not_supported"
        return result

    def _rank_flow_dataframe(self, df: Optional[pd.DataFrame], top_n: int) -> Dict[str, List[Dict[str, Any]]]:
        rankings: Dict[str, List[Dict[str, Any]]] = {"top": [], "bottom": []}
        if df is None or df.empty:
            return rankings
        name_col = _first_column_by_keywords(df, ["名称", "板块", "行业", "股票", "代码", "name"])
        flow_col = _first_column_by_keywords(df, ["主力净流入", "净流入", "净额", "资金流入", "flow"])
        if name_col is None or flow_col is None:
            return rankings
        work_df = df[[name_col, flow_col]].copy()
        work_df[flow_col] = pd.to_numeric(work_df[flow_col], errors="coerce")
        work_df = work_df.dropna(subset=[flow_col])
        if work_df.empty:
            return rankings
        rankings["top"] = [
            {"name": _safe_str(row[name_col]), "net_inflow": float(row[flow_col])}
            for _, row in work_df.nlargest(top_n, flow_col).iterrows()
        ]
        rankings["bottom"] = [
            {"name": _safe_str(row[name_col]), "net_inflow": float(row[flow_col])}
            for _, row in work_df.nsmallest(top_n, flow_col).iterrows()
        ]
        return rankings

    def _compact_northbound_history_row(self, row: Dict[str, Any]) -> Dict[str, Any]:
        series = pd.Series(row)
        return {
            "date": _row_date_value(series),
            **_compact_numeric_row(
                series,
                {
                    "net_inflow": ["净流入", "净买入", "当日成交净买额"],
                    "buy_amount": ["买入", "流入"],
                    "sell_amount": ["卖出", "流出"],
                    "balance": ["余额"],
                },
            ),
        }

    def _compact_margin_row(self, row: Dict[str, Any]) -> Dict[str, Any]:
        series = pd.Series(row)
        return {
            "date": _row_date_value(series),
            **_compact_numeric_row(
                series,
                {
                    "financing_balance": ["融资余额"],
                    "financing_buy_amount": ["融资买入额"],
                    "financing_repayment_amount": ["融资偿还额"],
                    "securities_lending_balance": ["融券余额"],
                    "margin_balance": ["融资融券余额", "两融余额"],
                },
            ),
        }

    def get_capital_flow(self, stock_code: str, top_n: int = 5) -> Dict[str, Any]:
        """
        Return stock capital flow.
        """
        result: Dict[str, Any] = {
            "status": "not_supported",
            "stock_flow": {},
            "sector_rankings": {"top": [], "bottom": []},
            "source_chain": [],
            "errors": [],
        }

        stockapi_flow, stockapi_source, stockapi_errors = self._get_stockapi_capital_flow(stock_code)
        if stockapi_flow:
            result["stock_flow"] = stockapi_flow
            result["source_chain"].append(f"capital_stock:{stockapi_source}")
            result["status"] = "partial"
            return result

        result["errors"].extend(stockapi_errors)
        if any(":not_supported:" in str(error) for error in stockapi_errors):
            result["status"] = "not_supported"
        elif result["errors"]:
            result["status"] = "failed"
        return result

    def _get_stockapi_capital_flow(self, stock_code: str) -> Tuple[Dict[str, Any], Optional[str], List[str]]:
        """
        Fetch historical stock capital flow from stockapi.com.cn.

        The endpoint is updated after market close and returns daily history, so it
        is used to fill the daily fields expected by get_capital_flow without
        touching Eastmoney individual fund-flow endpoints by default.
        """
        code = _normalize_code(stock_code)
        if not re.fullmatch(r"\d{6}", code or ""):
            return {}, None, [f"stockapi_codeFlow:not_supported:{stock_code}"]

        token = os.getenv("STOCKAPI_TOKEN", "").strip()
        today = datetime.now().date()
        windows: List[Tuple[datetime.date, datetime.date]] = []
        if token:
            end_date = today
            windows.append((end_date - timedelta(days=20), end_date))
        else:
            end_date = today - timedelta(days=4)
            window_end = end_date
            lower_bound = end_date - timedelta(days=14)
            while window_end >= lower_bound and len(windows) < 3:
                window_start = max(lower_bound, window_end - timedelta(days=4))
                windows.append((window_start, window_end))
                window_end = window_start - timedelta(days=1)

        rows: List[Any] = []
        errors: List[str] = []
        for start_date, window_end in windows:
            params: Dict[str, Any] = {
                "code": code,
                "startDate": start_date.isoformat(),
                "endDate": window_end.isoformat(),
                "pageNo": "1",
                "pageSize": "20",
            }
            if token:
                params["token"] = token

            try:
                response = requests.get(_stockapi_code_flow_url(), params=params, timeout=3.0)
                response.raise_for_status()
                payload = response.json()
            except Exception as exc:
                errors.append(f"stockapi_codeFlow:{type(exc).__name__}:{exc}")
                continue

            if not isinstance(payload, dict):
                errors.append("stockapi_codeFlow:invalid_response")
                continue
            if payload.get("code") != 20000:
                errors.append(f"stockapi_codeFlow:{payload.get('code')}:{payload.get('msg')}")
                continue

            data = payload.get("data")
            if isinstance(data, list):
                rows.extend(data)

        if not rows:
            if errors:
                return {}, None, errors
            return {}, None, ["stockapi_codeFlow:empty_data"]

        normalized_by_date: Dict[str, float] = {}
        for row in rows:
            if not isinstance(row, dict):
                continue
            amount = _safe_float(row.get("mainAmount"))
            date_text = _safe_str(row.get("date"))
            if amount is None or not date_text:
                continue
            normalized_by_date[date_text] = amount
        normalized_rows = sorted(normalized_by_date.items(), key=lambda item: item[0])
        if not normalized_rows:
            return {}, None, ["stockapi_codeFlow:no_main_amount"]

        amounts = [item[1] for item in normalized_rows]
        latest_date, latest_main = normalized_rows[-1]
        return {
            "main_net_inflow": latest_main,
            "inflow_5d": float(sum(amounts[-5:])),
            "inflow_10d": float(sum(amounts[-10:])),
            "latest_date": latest_date,
            "source_update": "after_market_close",
        }, "stockapi_codeFlow", []

    def get_dragon_tiger_flag(self, stock_code: str, lookback_days: int = 20) -> Dict[str, Any]:
        """
        Return dragon-tiger signal in lookback window.
        """
        result: Dict[str, Any] = {
            "status": "not_supported",
            "is_on_list": False,
            "recent_count": 0,
            "latest_date": None,
            "source_chain": [],
            "errors": [],
        }

        df, source, errors = self._call_df_candidates([
            ("stock_lhb_stock_statistic_em", {}),
            ("stock_lhb_detail_em", {}),
            ("stock_lhb_jgmmtj_em", {}),
        ])
        result["errors"].extend(errors)
        if df is None:
            return result

        # Try code filter
        code_cols = [c for c in df.columns if any(k in str(c) for k in ("代码", "股票代码", "证券代码"))]
        target = _normalize_code(stock_code)
        matched = pd.DataFrame()
        for col in code_cols:
            try:
                series = df[col].astype(str).map(_normalize_code)
                cur = df[series == target]
                if not cur.empty:
                    matched = cur
                    break
            except Exception:
                continue
        if matched.empty:
            result["source_chain"].append(f"dragon_tiger:{source}")
            result["status"] = "ok" if code_cols else "partial"
            return result

        date_col = next((c for c in matched.columns if any(k in str(c) for k in ("日期", "上榜", "交易日", "time"))), None)
        parsed_dates: List[datetime] = []
        if date_col is not None:
            for val in matched[date_col].astype(str).tolist():
                try:
                    parsed_dates.append(pd.to_datetime(val).to_pydatetime())
                except Exception:
                    continue
        now = datetime.now()
        start = now - timedelta(days=max(1, lookback_days))
        recent_dates = [d for d in parsed_dates if start <= d <= now]

        result["is_on_list"] = bool(recent_dates)
        result["recent_count"] = len(recent_dates) if recent_dates else int(len(matched))
        result["latest_date"] = max(recent_dates).date().isoformat() if recent_dates else (
            max(parsed_dates).date().isoformat() if parsed_dates else None
        )
        result["status"] = "ok"
        result["source_chain"].append(f"dragon_tiger:{source}")
        return result
