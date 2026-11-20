# -*- coding: utf-8 -*-
"""按 ``(symbol, as_of)`` 还原历史 A 股身份元数据.

``current_cn_common_stock_metadata`` 只回答"当前有效交易日"的身份; 历史
``as_of`` 会返回 ``None``, 导致 ``PricePlanReadinessGate`` 判
``system_unready``, 绕过闸门后也会落 ``MARKET_RULES_UNAVAILABLE``。本模块用
本地快照补齐历史分支:

- ``stock_basic`` (``list_date`` / ``delist_date`` / ``list_status``) →
  上市状态, 以及标的是否为 A 股普通股。
- ``namechange`` → 在 ``as_of`` 当日生效的证券简称, 用于反推 ST 状态。

数据加载复用 ``src.agent.candidate_experts_v2.hard_risk`` 的 loader, 本模块
只做"按日期取值"的推导, 不新增第二套读取实现。逐次读取会产生可感知的解析
开销, 因此按文件路径 + mtime 做进程内缓存。

重要限制 (如实说明, 不做精确性包装):

- ``namechange_snapshot.csv`` 被上游单次返回上限截断在 2006-10-09 (恰好
  10,000 行, 按 ``start_date`` 降序)。2006-10-09 之后的更名记录是完整的;
  因此某代码在快照中**没有任何记录**时, 可视为"2006-10-09 之后未更名", 用
  ``stock_basic.name`` 作为该日期名称 (verified=True)。
- 若代码有更名记录, 但最早的记录晚于 ``as_of`` (即更名前的名称不在快照里),
  ``st_status_verified=False``, 交由 readiness 判未就绪, 不做猜测。
- 退市股按 ``delist_date`` 判 ``delisted``; 只有 ``list_status == "D"`` 而缺少
  ``delist_date`` 时无法定位退市日, 返回 ``unknown`` 且未验证。
- ST 只能从 ``namechange`` 反推, 与真实交易状态存在误差; 快照被截断或上游
  漏记时同样存在误差, 这里只保证"有据可依", 不保证"与交易所逐日一致"。

本模块只处理严格早于"当前有效交易日"的日期; 当前/未来日期一律返回 ``None``,
保证线上默认路径仍走 ``current_cn_common_stock_metadata``。
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

from src.agent.candidate_experts_v2.hard_risk import (
    DEFAULT_CACHE_DIR,
    NAMECHANGE_CACHE,
    STOCK_BASIC_CACHE,
    _normalize_date,
    load_namechange_map,
    load_stock_basic_map,
)

from .market_rules import is_st_stock_name, normalize_cn_symbol

_SNAPSHOT_CACHE: dict[tuple[str, int, str, int], tuple[dict, dict | None]] = {}
_SNAPSHOT_CACHE_LIMIT = 4


def historical_cn_common_stock_metadata(
    symbol: str,
    *,
    trade_date: date,
    cache_dir: Path | str | None = None,
) -> dict[str, object] | None:
    """Resolve stock identity for a historical ``trade_date``.

    Returns ``None`` when the date is not strictly before the current effective
    CN session (the live path owns it), or when ``stock_basic`` has no row for
    the symbol. A partially verified payload is returned when some fields can
    be dated but others cannot; ``*_verified=False`` keeps readiness closed
    instead of silently treating the gap as safe.
    """
    from src.core.trading_calendar import get_effective_trading_date, get_market_now

    digits = normalize_cn_symbol(symbol)
    if not digits:
        return None
    current_session = get_effective_trading_date(
        "cn",
        current_time=get_market_now("cn"),
    )
    if trade_date >= current_session:
        return None
    stock_basic, namechange = _load_snapshots(cache_dir)
    row = stock_basic.get(digits)
    if not row:
        return None
    instrument_type, instrument_type_verified = _instrument_type(digits)
    listing_status, listing_status_verified = _listing_status(row, trade_date)
    st_status, st_status_verified = _st_status(
        digits,
        row,
        namechange,
        trade_date,
    )
    return {
        "st_status": st_status,
        "st_status_verified": st_status_verified,
        "listing_status": listing_status,
        "listing_status_verified": listing_status_verified,
        "instrument_type": instrument_type,
        "instrument_type_verified": instrument_type_verified,
        "metadata_observed_on": trade_date,
    }


def _load_snapshots(
    cache_dir: Path | str | None,
) -> tuple[dict, dict | None]:
    base = Path(cache_dir) if cache_dir is not None else DEFAULT_CACHE_DIR
    stock_basic_path = base / STOCK_BASIC_CACHE
    namechange_path = base / NAMECHANGE_CACHE
    key = (
        str(stock_basic_path),
        _mtime_ns(stock_basic_path),
        str(namechange_path),
        _mtime_ns(namechange_path),
    )
    cached = _SNAPSHOT_CACHE.get(key)
    if cached is not None:
        return cached
    stock_basic, _ = load_stock_basic_map(cache_dir=base, allow_online=False)
    namechange, _ = load_namechange_map(cache_dir=base, allow_online=False)
    if len(_SNAPSHOT_CACHE) >= _SNAPSHOT_CACHE_LIMIT:
        _SNAPSHOT_CACHE.clear()
    _SNAPSHOT_CACHE[key] = (stock_basic, namechange)
    return stock_basic, namechange


def _mtime_ns(path: Path) -> int:
    try:
        return path.stat().st_mtime_ns
    except OSError:
        return -1


def _instrument_type(digits: str) -> tuple[str, bool]:
    from src.agent.candidate_experts_v2.instruments import (
        A_SHARE,
        UNKNOWN,
        classify_instrument,
    )

    instrument_type, _ = classify_instrument(digits)
    if instrument_type == A_SHARE:
        return "common_stock", True
    return instrument_type, instrument_type != UNKNOWN


def _listing_status(row: dict[str, str], trade_date: date) -> tuple[str, bool]:
    list_date = _as_date(row.get("list_date"))
    delist_date = _as_date(row.get("delist_date"))
    current_status = str(row.get("list_status") or "").strip().upper()
    if delist_date is not None:
        return ("delisted", True) if trade_date >= delist_date else ("listed", True)
    if current_status == "D":
        # Delisted without a usable delist date: the listing window cannot be
        # dated, so do not claim "listed" for any trade date.
        return "unknown", False
    if list_date is None:
        return "unknown", False
    if trade_date < list_date:
        return "not_listed", True
    return "listed", True


def _st_status(
    digits: str,
    row: dict[str, str],
    namechange: dict[str, list[dict[str, str]]] | None,
    trade_date: date,
) -> tuple[bool, bool]:
    fallback_name = row.get("name")
    if namechange is None:
        # The source itself is unavailable; keep the best-effort name but never
        # present it as verified.
        return is_st_stock_name(fallback_name), False
    records = namechange.get(digits) or []
    if not records:
        # See module docstring: the snapshot is complete after 2006-10-09, so a
        # code with no records did not rename, and its listed name still holds.
        return is_st_stock_name(fallback_name), True
    covering = [record for record in records if _covers(record, trade_date)]
    if covering:
        latest = max(covering, key=lambda item: _as_date(item.get("start_date")) or trade_date)
        return is_st_stock_name(latest.get("name")), True
    # The name in force before the first recorded rename is not in the
    # snapshot (or the record set has a gap); keep the fallback name but never
    # present it as verified.
    return is_st_stock_name(fallback_name), False


def _covers(record: dict[str, str], trade_date: date) -> bool:
    start = _as_date(record.get("start_date"))
    end = _as_date(record.get("end_date"))
    if start is None or start > trade_date:
        return False
    return end is None or end >= trade_date


def _as_date(value: object) -> date | None:
    text = _normalize_date(value)
    if not text:
        return None
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None
