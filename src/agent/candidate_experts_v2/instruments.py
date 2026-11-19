# -*- coding: utf-8 -*-
"""A 股标的类型识别（选股链路 P2 前置过滤）.

选股链路只处理沪深京 A 股。可转债、ETF/基金、债券、逆回购、B 股等虽然也是
6 位数字代码，但不能进入 seed pool / 候选判定，也不能按"未知股票"处理。

识别仅依赖代码段（无需网络/缓存），保证在 stock_basic 缺记录时仍能给出明确的
标的类型；北交所 43/83/87/88/920 段视为 A 股，避免误伤。
"""

from __future__ import annotations

from typing import Dict, Tuple

InstrumentType = str

A_SHARE = "a_share"
CONVERTIBLE_BOND = "convertible_bond"
ETF = "etf"
FUND = "fund"
BOND = "bond"
REPO = "repo"
B_SHARE = "b_share"
UNKNOWN = "unknown"

INSTRUMENT_LABELS: Dict[str, str] = {
    A_SHARE: "A股股票",
    CONVERTIBLE_BOND: "可转债",
    ETF: "ETF",
    FUND: "基金",
    BOND: "债券",
    REPO: "债券回购",
    B_SHARE: "B股",
    UNKNOWN: "未知标的",
}

# 沪深主板 / 创业板 / 科创板 / 北交所
_A_SHARE_PREFIXES = (
    "600", "601", "603", "605", "688", "689",   # 沪市 A 股
    "000", "001", "002", "003", "300", "301", "302",   # 深市 A 股
    "43", "83", "87", "88", "920",              # 北交所
)
_CONVERTIBLE_PREFIXES = (
    "110", "111", "113", "118",                 # 沪市可转债
    "123", "127", "128",                        # 深市可转债
)
_B_SHARE_PREFIXES = ("200", "900")              # 深市 B / 沪市 B
_ETF_PREFIXES = ("15", "51", "56", "58")        # 深市 ETF(159) / 沪市 ETF(51x/56x/58x)
_FUND_PREFIXES = ("16", "18", "50", "52")       # LOF/封闭式基金
_REPO_PREFIXES = ("131", "204")                 # 深市 / 沪市债券回购
_BOND_PREFIXES = ("019", "10", "11", "12", "13", "14", "19")  # 国债/企债/公司债等


def _code6(value: object) -> str:
    text = str(value or "").strip()
    return text.split(".")[0].strip()


def classify_instrument(code: object) -> Tuple[str, str]:
    """Return ``(instrument_type, label)`` for a 6-digit code.

    Non-6-digit codes (e.g. HK/US symbols) return ``unknown``.  Prefix order
    matters: A 股 first (so 000xxx stocks are not treated as indexes), then
    可转债, then B 股, then ETF/基金/债券.
    """
    text = _code6(code)
    if len(text) != 6 or not text.isdigit():
        return UNKNOWN, INSTRUMENT_LABELS[UNKNOWN]
    if text.startswith(_A_SHARE_PREFIXES):
        return A_SHARE, INSTRUMENT_LABELS[A_SHARE]
    if text.startswith(_CONVERTIBLE_PREFIXES):
        return CONVERTIBLE_BOND, INSTRUMENT_LABELS[CONVERTIBLE_BOND]
    if text.startswith(_B_SHARE_PREFIXES):
        return B_SHARE, INSTRUMENT_LABELS[B_SHARE]
    if text.startswith(_ETF_PREFIXES):
        return ETF, INSTRUMENT_LABELS[ETF]
    if text.startswith(_FUND_PREFIXES):
        return FUND, INSTRUMENT_LABELS[FUND]
    if text.startswith(_REPO_PREFIXES):
        return REPO, INSTRUMENT_LABELS[REPO]
    if text.startswith(_BOND_PREFIXES):
        return BOND, INSTRUMENT_LABELS[BOND]
    return UNKNOWN, INSTRUMENT_LABELS[UNKNOWN]


def is_a_share(code: object) -> bool:
    return classify_instrument(code)[0] == A_SHARE
