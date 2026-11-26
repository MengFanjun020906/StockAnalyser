# -*- coding: utf-8 -*-
"""A 股名称字典（扩展版）与文本名称匹配。

默认字典来自 ``data/factor_eval_cache/stock_basic_LD.csv``（约 5,924 条，含退市），
可通过 ``NEWS_NO_STOCK_TRIAGE_STOCK_BASIC_PATH`` 覆盖；文件缺失或读取失败时回退到
``src/data/stock_mapping.py`` 的 ``STOCK_NAME_MAP`` A 股子集。

只做“名称 → 6 位代码”的确定性匹配，不参与选股、打分、排序或提示词。
"""

from __future__ import annotations

import csv
import logging
from pathlib import Path
from threading import RLock
from typing import Dict, Iterable, Iterator, Optional, Tuple

from src.data.stock_mapping import STOCK_NAME_MAP

logger = logging.getLogger(__name__)

_REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_STOCK_BASIC_LD_PATH = _REPO_ROOT / "data" / "factor_eval_cache" / "stock_basic_LD.csv"

# 3~4 字通用词与常见股票简称重名，直接排除，避免“机器人/农产品/太阳能/大西洋”这类误命中。
GENERIC_STOCK_NAME_STOPLIST = frozenset(
    {
        "机器人",
        "农产品",
        "太阳能",
        "大西洋",
        "太平洋",
        "新世界",
        "新大陆",
        "新希望",
        "全新好",
        "数字人",
        "指南针",
        "向日葵",
        "黑芝麻",
        "红太阳",
        "金太阳",
        "老百姓",
        "三人行",
        "万事利",
        "大东方",
        "大东南",
        "天下秀",
        "红四方",
        "红蜻蜓",
        "中关村",
        "金融街",
        "陆家嘴",
        "外高桥",
        "王府井",
        "张家界",
        "长白山",
        "中国中期",
        "国际实业",
        "美丽生态",
        "人民网",
        "新华网",
        "大智慧",
    }
)

_NAME_MAP_CACHE: Dict[str, Dict[str, str]] = {}
_MATCHER_CACHE: Dict[str, "AStockNameMatcher"] = {}
_CACHE_LOCK = RLock()


def resolve_stock_basic_path(path: Optional[str] = None) -> Path:
    """空值用默认路径；相对路径按仓库根解析，绝对路径原样返回。"""

    candidate = str(path or "").strip()
    if not candidate:
        return DEFAULT_STOCK_BASIC_LD_PATH
    resolved = Path(candidate)
    if resolved.is_absolute():
        return resolved
    return _REPO_ROOT / resolved


def _normalize_a_share_code(ts_code: str) -> Optional[str]:
    """复用事件地基的代码归一化，避免两套实现漂移（延迟导入防循环依赖）。"""

    from src.services.news_event_foundation import normalize_code

    return normalize_code(ts_code)


def _is_usable_name(name: str) -> bool:
    text = str(name or "").strip()
    if len(text) < 3:
        return False
    return text not in GENERIC_STOCK_NAME_STOPLIST


def _load_a_share_name_map_from_csv(path: Path) -> Dict[str, str]:
    mapping: Dict[str, str] = {}
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            code = _normalize_a_share_code(str(row.get("ts_code") or ""))
            name = str(row.get("name") or "").strip()
            if not code or code in mapping or not _is_usable_name(name):
                continue
            mapping[code] = name
    return mapping


def _fallback_a_share_name_map() -> Dict[str, str]:
    mapping: Dict[str, str] = {}
    for code, name in STOCK_NAME_MAP.items():
        normalized = _normalize_a_share_code(code)
        if not normalized or normalized in mapping or not _is_usable_name(str(name)):
            continue
        mapping[normalized] = str(name).strip()
    return mapping


def load_a_share_name_map(path: Optional[str] = None) -> Dict[str, str]:
    """加载 A 股名称字典；文件不可用时回退内置 A 股子集。"""

    resolved = resolve_stock_basic_path(path)
    if not resolved.is_file():
        logger.debug("[股票名称] 未找到 %s，回退内置 STOCK_NAME_MAP", resolved)
        return _fallback_a_share_name_map()
    try:
        return _load_a_share_name_map_from_csv(resolved)
    except (OSError, UnicodeDecodeError, csv.Error) as exc:
        logger.debug("[股票名称] 读取 %s 失败，回退内置 STOCK_NAME_MAP: %s", resolved, exc)
        return _fallback_a_share_name_map()


class AStockNameMatcher:
    """首字索引匹配器，避免 5,924×N 的全量扫描。"""

    def __init__(self, name_map: Dict[str, str]) -> None:
        self.name_map = dict(name_map)
        index: Dict[str, list[Tuple[str, str]]] = {}
        for code, name in self.name_map.items():
            if not name:
                continue
            index.setdefault(name[0], []).append((code, name))
        # 长名优先，减少“中国银行”被“中国银”类短名遮挡的概率。
        self._index: Dict[str, Tuple[Tuple[str, str], ...]] = {
            head: tuple(sorted(items, key=lambda item: len(item[1]), reverse=True))
            for head, items in index.items()
        }

    def find(self, text: str) -> Iterator[Tuple[str, str]]:
        if not text:
            return
        seen: set[str] = set()
        for position, char in enumerate(text):
            for code, name in self._index.get(char, ()):
                if code in seen:
                    continue
                if text.startswith(name, position):
                    seen.add(code)
                    yield code, name


def get_a_share_name_matcher(path: Optional[str] = None) -> AStockNameMatcher:
    """按绝对路径缓存匹配器，供新闻入库反复调用。"""

    key = str(resolve_stock_basic_path(path))
    with _CACHE_LOCK:
        cached = _MATCHER_CACHE.get(key)
        if cached is not None:
            return cached
        mapping = _NAME_MAP_CACHE.get(key)
        if mapping is None:
            mapping = load_a_share_name_map(key)
            _NAME_MAP_CACHE[key] = mapping
        matcher = AStockNameMatcher(mapping)
        _MATCHER_CACHE[key] = matcher
        logger.debug("[股票名称] 已加载 %s：%d 条可用名称", key, len(mapping))
        return matcher


def get_a_share_name_map(path: Optional[str] = None) -> Dict[str, str]:
    return get_a_share_name_matcher(path).name_map


def iter_name_matches(
    text: str,
    name_map: Optional[Dict[str, str]] = None,
) -> Iterable[Tuple[str, str]]:
    """按首字索引返回文本中命中的 ``(code, name)``，isomorphic 于匹配器。"""

    if not text:
        return ()
    matcher = AStockNameMatcher(name_map) if name_map is not None else get_a_share_name_matcher()
    return tuple(matcher.find(text))


def reset_stock_name_cache() -> None:
    """测试与热重载用：清空名称字典与匹配器缓存。"""

    with _CACHE_LOCK:
        _NAME_MAP_CACHE.clear()
        _MATCHER_CACHE.clear()
