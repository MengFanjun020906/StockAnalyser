# -*- coding: utf-8 -*-
"""无个股新闻（``stock_scope=no_stock``）分类与优先级排序。

本模块只做确定性的“分类 + 排序”：

- 分类：按报告 §6.2–§6.4 的关键词与地区规则，把一条 ``no_stock`` 新闻归到
  ``us_index`` / ``us_chain`` / ``jpkr_chain`` / ``commodity`` / ``industrial_policy`` /
  ``major_macro`` 等通道，或归到硬丢弃 / 仅展示类别。
- 打分：**分数完全由实测效应量推导**，公式见 ``score_from_t``；每条规则都指向
  ``EVIDENCE`` 里一条带 β / t / R² / 出处 的证据记录。无实测或实测不显著的类别
  分数为 0，只保留展示，不参与任何决策。
- 排序：``triage_many`` 仅按 ``priority`` / ``score`` 降序排列，不修改任何既有评分、
  阈值、gate、prompt 或选股结果。

数据来源：``docs/research/no-stock-news-triage.md`` §4（自有数据事件研究）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from src.data.stock_name_index import AStockNameMatcher

# 标尺取“真正有次日预测力”的最强证据：纳指 → 电子超额缺口，β=0.437, t=6.8, R²=5.2%。
# 不采用沪铜同期的 t=6.9：那是同日同步相关，滞后一日 β 不显著，不能当作次日预测标尺。
T_MAX = 6.8
# 双侧 5% 显著性阈值（|t| ≈ 1.96）。
MIN_T = 2.0


def score_from_t(t: Optional[float]) -> int:
    """把实测 |t| 线性映射到 0–100；|t| 低于显著性阈值记 0。"""

    if t is None or abs(float(t)) < MIN_T:
        return 0
    return round(100 * min(1.0, abs(float(t)) / T_MAX))


@dataclass(frozen=True)
class Evidence:
    """一条可追溯的实测证据。``zero_reason`` 非空时分数强制为 0。"""

    key: str
    label: str
    beta: Optional[float]
    t: Optional[float]
    r2: Optional[float]
    sample_n: Optional[int]
    source: str
    evidence_strength: str
    zero_reason: str = ""

    @property
    def score(self) -> int:
        if self.zero_reason:
            return 0
        return score_from_t(self.t)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "key": self.key,
            "label": self.label,
            "beta": self.beta,
            "t": self.t,
            "r2": self.r2,
            "sample_n": self.sample_n,
            "source": self.source,
            "evidence_strength": self.evidence_strength,
            "score": self.score,
            "zero_reason": self.zero_reason,
        }


_COMMODITY_ZERO_REASON = "商品滞后一日 β 不显著/反向（金 -2.3、铜 -1.5、螺纹 -0.8、锂 -1.1、油 -0.5）；同期 β 仅作参考，不参与打分"


EVIDENCE: Dict[str, Evidence] = {
    "us_index_spx": Evidence("us_index_spx", "标普500 → A股开盘缺口", 0.263, 6.5, 0.047, 850, "报告 §4.2", "external+own_data"),
    "us_index_ndx": Evidence("us_index_ndx", "纳斯达克 → A股开盘缺口", 0.167, 5.6, 0.036, 850, "报告 §4.2", "external+own_data"),
    "us_index_dji": Evidence("us_index_dji", "道指 → A股开盘缺口", 0.274, 6.1, 0.042, 850, "报告 §4.2", "external+own_data"),
    "us_chain": Evidence("us_chain", "纳指 → 电子超额缺口", 0.437, 6.8, 0.052, 850, "报告 §4.3", "external+own_data"),
    "us_tech_telecom": Evidence("us_tech_telecom", "纳指 → 通信超额缺口", 0.387, 6.4, 0.045, 850, "报告 §4.3", "external+own_data"),
    "us_tech_computer": Evidence("us_tech_computer", "纳指 → 计算机超额缺口", 0.299, 4.7, 0.026, 850, "报告 §4.3", "external+own_data"),
    "us_tech_power_equipment": Evidence("us_tech_power_equipment", "纳指 → 电力设备超额缺口", 0.205, 3.8, 0.017, 850, "报告 §4.3", "external+own_data"),
    "jpkr_n225_electronics": Evidence("jpkr_n225_electronics", "日经 → 电子超额缺口", 0.146, 2.8, 0.009, 850, "报告 §4.3", "own_data"),
    "jpkr_n225_machinery": Evidence("jpkr_n225_machinery", "日经 → 机械设备超额缺口", 0.149, 3.4, 0.013, 850, "报告 §4.3", "own_data"),
    "jpkr_n225_auto": Evidence("jpkr_n225_auto", "日经 → 汽车超额缺口", 0.127, 3.1, 0.011, 850, "报告 §4.3", "own_data"),
    "jpkr_kospi_electronics": Evidence("jpkr_kospi_electronics", "KOSPI → 电子超额缺口", 0.105, 2.8, 0.009, 850, "报告 §4.3", "own_data"),
    "commodity_copper": Evidence("commodity_copper", "沪铜 → 有色金属超额（同期，仅参考）", 0.577, 6.9, 0.054, 850, "报告 §4.5", "own_data", zero_reason=_COMMODITY_ZERO_REASON),
    "commodity_gold": Evidence("commodity_gold", "黄金 → 有色金属超额（同期，仅参考）", 0.346, 5.2, 0.031, 850, "报告 §4.5", "own_data", zero_reason=_COMMODITY_ZERO_REASON),
    "commodity_lithium": Evidence("commodity_lithium", "碳酸锂 → 有色金属超额（同期，仅参考）", 0.152, 4.8, 0.027, 850, "报告 §4.5", "own_data", zero_reason=_COMMODITY_ZERO_REASON),
    "commodity_rebar": Evidence("commodity_rebar", "螺纹钢 → 钢铁超额（同期，仅参考）", 0.257, 3.0, 0.011, 850, "报告 §4.5", "own_data", zero_reason=_COMMODITY_ZERO_REASON),
    "commodity_oil": Evidence("commodity_oil", "原油 → 石油石化超额（同期，仅参考）", 0.061, 2.2, 0.006, 850, "报告 §4.5", "own_data", zero_reason=_COMMODITY_ZERO_REASON),
    # —— 以下为实测不显著或缺乏实测依据，分数强制为 0，仅保留展示 ——
    "europe": Evidence(
        "europe", "欧洲市场 → A股开盘缺口", 0.165, 4.0, 0.019, 850,
        "报告 §4.2：单变量 t=4.0；控制 SPX 后 t=1.9、ΔR²≈0.4pct",
        "own_data",
        zero_reason="控制标普后增量 t=1.9 < 2.0，ΔR²≈0.4pct，视为全球因子重复",
    ),
    "commodity_lag": Evidence(
        "commodity_lag", "商品滞后一日 → 行业超额", None, None, None, 850,
        "报告 §4.5：铜 t=-1.5、金 t=-2.3、螺纹 t=-0.8、锂 t=-1.1、油 t=-0.5",
        "own_data",
        zero_reason="滞后一日 β 全部不显著或方向相反",
    ),
    "macro_lpr": Evidence("macro_lpr", "LPR 发布日指数收益", None, -0.26, None, 42, "报告 §4.6", "external+own_data",
                          zero_reason="事件日 t=-0.26，不显著"),
    "macro_pmi": Evidence("macro_pmi", "PMI 发布日指数收益", None, 0.15, None, 41, "报告 §4.6", "external+own_data",
                          zero_reason="事件日 t=0.15，不显著"),
    "macro_cpi": Evidence("macro_cpi", "CPI 发布日指数收益", None, -0.14, None, 42, "报告 §4.6", "external+own_data",
                          zero_reason="事件日 t=-0.14，不显著"),
    "macro_credit": Evidence("macro_credit", "社融/M2 发布", None, None, None, None, "报告 §5.2", "inference",
                             zero_reason="未单独检验，仅有外部文献"),
    "macro_overseas": Evidence("macro_overseas", "美债/美元/汇率", None, None, None, None, "报告 §5.2", "inference",
                               zero_reason="未单独显著，仅作风险偏好背景"),
    "industrial_policy": Evidence("industrial_policy", "重大产业政策", None, None, None, None, "报告 §3.3 / §5.1", "external",
                                  zero_reason="有外部文献支持，但无自有实测效应量"),
    "major_macro": Evidence("major_macro", "降准降息/政治局/国常会", None, None, None, None, "报告 §3.4 / §5.2", "inference",
                            zero_reason="仅外部文献与推断，无自有实测"),
    "sector_generic": Evidence("sector_generic", "泛行业/板块描述", None, None, None, None, "报告 §5.2", "inference",
                               zero_reason="无明确行业主体或方向，仅推断"),
    "index_recap": Evidence("index_recap", "指数复盘/收评", None, None, None, None, "报告 §5.3", "own_data",
                            zero_reason="同日收盘描述，结构上无次日增量信息"),
    "commodity_generic": Evidence("commodity_generic", "未具名商品", None, None, None, None, "报告 §4.5", "own_data",
                                  zero_reason="未映射到具名品类，无法对应实测 β"),
    "jpkr_generic": Evidence("jpkr_generic", "日韩泛市场", None, None, None, None, "报告 §4.3", "own_data",
                             zero_reason="宽基增量≈0，需行业关键词才显著"),
    "policy_routine": Evidence("policy_routine", "例行财政/监管事务", None, None, None, None, "报告 §5.3", "inference",
                               zero_reason="例行事务无增量信息、无可交易映射"),
    "other": Evidence("other", "其他/未分类", None, None, None, None, "报告 §5.3", "none",
                      zero_reason="无稳定映射"),
    "anonymous": Evidence("anonymous", "匿名指代主体", None, None, None, None, "报告 §6.2", "inference"),
    "column": Evidence("column", "匿名研报体例", None, None, None, None, "报告 §6.2", "inference"),
    "digest": Evidence("digest", "聚合/复盘", None, None, None, None, "报告 §6.2", "inference"),
    "nonmarket": Evidence("nonmarket", "天气/社会/司法", None, None, None, None, "报告 §6.2", "inference"),
    "other_region": Evidence("other_region", "其他地区/单一产地", None, None, None, None, "报告 §6.3", "inference"),
    "geopolitics": Evidence("geopolitics", "地缘政治（非能源供给）", None, None, None, None, "报告 §6.3", "inference"),
}


@dataclass(frozen=True)
class Classification:
    category: str
    evidence_key: str
    channel: str
    keep: bool
    drop_reason: str = ""
    mapped_sw_l1: Tuple[str, ...] = ()
    subject_resolved: bool = False


_ANONYMOUS_RE = re.compile(r"(这家公司|该公司|某公司|这两家企业|这几家企业|三家企业|另一家|上述公司|该企业)")
_COLUMN_RE = re.compile(r"^【(风口研报|电报解读|盘中宝|机构调研|机构龙虎榜解读|公告全知道|研选|玩转ETF|点金互动易|九点特供|金牌纪要库|财联社早知道|掘金行业龙头)")
_CODE_RE = re.compile(r"\d{6}")

_DIGEST_WORDS = ("新闻精选", "早知道", "隔夜要闻", "复盘", "要闻", "盘前必读")
_NONMARKET_WORDS = ("暴雪", "暴雨", "台风", "寒潮", "地震", "洪水", "干旱", "预警", "枪击", "车祸", "火灾", "坠机", "伤亡", "判刑", "双开", "逮捕")

_US_INDEX_RULES: Tuple[Tuple[str, Tuple[str, ...]], ...] = (
    ("us_index_ndx", ("纳指", "纳斯达克")),
    ("us_index_dji", ("道指", "道琼斯")),
    ("us_index_spx", ("标普", "标普500", "美股", "美国股市")),
)

_US_CHAIN_RULES: Tuple[Tuple[str, Tuple[str, ...], Tuple[str, ...], Tuple[str, ...]], ...] = (
    ("us_tech_telecom", ("通信",), ("Coherent", "coherent", "Lumentum", "lumentum", "光模块", "光通信"), ()),
    ("us_tech_computer", ("计算机",), ("微软", "谷歌", "亚马逊", "Meta", "meta", "云计算", "AI应用"), ()),
    ("us_tech_power_equipment", ("电力设备",), ("储能", "光伏", "逆变器", "电网"), ()),
    (
        "us_chain",
        ("电子", "汽车"),
        ("英伟达", "AMD", "amd", "博通", "美光", "台积电", "费城半导体", "半导体", "芯片", "苹果", "特斯拉"),
        ("期货", "现货", "主连", "合约", "果价", "水果"),  # 排除“苹果期货”等农产品语境
    ),
)

_JPKR_WORDS = ("日经", "日本央行", "日本", "韩国", "KOSPI", "三星电子", "SK海力士", "丰田", "本田", "日产", "现代")
_JPKR_INDUSTRY_RULES: Tuple[Tuple[str, Tuple[str, ...], Tuple[str, ...]], ...] = (
    ("jpkr_kospi_electronics", ("电子",), ("三星电子", "SK海力士", "KOSPI", "韩国半导体")),
    ("jpkr_n225_machinery", ("机械设备",), ("机床", "工业机器人", "发那科", "机械")),
    ("jpkr_n225_auto", ("汽车",), ("丰田", "本田", "日产", "汽车")),
    ("jpkr_n225_electronics", ("电子",), ("日经", "东京电子", "索尼", "半导体", "存储")),
)

_EUROPE_WORDS = ("欧洲", "欧元区", "德国", "法国", "英国", "意大利", "西班牙", "荷兰", "瑞士", "欧盟", "DAX", "富时", "CAC", "欧央行")
_OTHER_REGION_WORDS = ("印度", "越南", "印尼", "菲律宾", "泰国", "马来西亚", "新加坡", "巴西", "阿根廷", "墨西哥", "加拿大", "澳大利亚", "新西兰", "南非", "埃及", "土耳其", "沙特", "阿联酋", "巴基斯坦", "非洲", "南美", "拉美", "智利", "秘鲁")
_GEOPOLITICS_WORDS = ("俄乌", "乌克兰", "俄罗斯", "以色列", "伊朗", "哈马斯", "中东", "胡塞", "空袭", "导弹", "无人机", "冲突", "制裁")
_ENERGY_SUPPLY_WORDS = ("原油", "天然气", "炼油", "霍尔木兹", "油田", "油轮", "管道", "北溪", "能源")

_COMMODITY_RULES: Tuple[Tuple[str, Tuple[str, ...], Tuple[str, ...]], ...] = (
    ("commodity_copper", ("有色金属",), ("铜价", "沪铜", "精炼铜", "铜矿", "铜")),
    ("commodity_gold", ("有色金属",), ("黄金", "金价", "沪金", "伦敦金")),
    ("commodity_lithium", ("有色金属",), ("碳酸锂", "锂价", "锂矿", "锂")),
    ("commodity_rebar", ("钢铁",), ("螺纹钢", "钢材", "铁矿石", "钢价")),
    ("commodity_oil", ("石油石化",), ("原油", "布伦特", "WTI", "油价", "美油", "布油")),
)
_COMMODITY_GENERIC_WORDS = ("大宗商品", "商品期货", "商品价格", "商品指数", "期货", "大宗", "多晶硅", "有色", "钯金", "铂金", "白银")

_CENTRAL_WORDS = ("国务院", "发改委", "工信部", "财政部", "中央", "国务院常务会议")
_POLICY_TOOL_WORDS = ("规划", "行动方案", "专项", "补贴", "税收优惠", "基金", "试点", "意见", "通知", "支持")
_POLICY_INDUSTRY_WORDS = ("半导体", "芯片", "新能源汽车", "光伏", "储能", "人工智能", "算力", "机器人", "医药", "军工", "低空经济", "数据要素", "核电", "氢能")
_POLICY_ROUTINE_WORDS = ("国债招标", "承销团", "招标", "备案", "受理", "批准", "处罚", "警示函", "地方项目")

_MACRO_RULES: Tuple[Tuple[str, Tuple[str, ...]], ...] = (
    ("macro_lpr", ("LPR", "贷款市场报价利率")),
    ("macro_pmi", ("PMI", "采购经理")),
    ("macro_cpi", ("CPI", "居民消费价格")),
    ("macro_credit", ("社融", "社会融资", "M2", "信贷")),
    ("major_macro", ("降准", "降息", "政治局会议", "国常会", "国务院常务会议", "中央经济工作会议", "中央金融工作会议")),
    ("macro_overseas", ("美联储", "美债", "美元指数", "汇率", "人民币")),
)

_INDEX_RECAP_WORDS = ("收评", "收盘", "成交额", "指数报", "指数收", "沪指", "深成指", "创业板指", "两市")
_SECTOR_GENERIC_WORDS = ("板块", "行业", "概念")

_DIRECTION_POSITIVE = ("上涨", "大涨", "暴涨", "创新高", "新高", "上调", "提高", "超预期", "好于预期", "放宽", "降息", "降准", "增持", "回购", "增长", "回暖", "突破", "涨价", "紧缺", "涨")
_DIRECTION_NEGATIVE = ("下跌", "大跌", "暴跌", "创新低", "新低", "下调", "降低", "不及预期", "低于预期", "收紧", "加息", "减持", "亏损", "下滑", "萎缩", "降价", "过剩", "违约", "跌")


def _contains(text: str, words: Iterable[str]) -> bool:
    return any(word and word in text for word in words)


def _build_text(*parts: Any) -> str:
    return " ".join(str(part or "") for part in parts)


def _classify(text: str, title: str) -> Classification:
    # 1) 硬丢弃：匿名指代 / 研报体例 / 聚合复盘 / 天气社会
    if _ANONYMOUS_RE.search(text) and not _CODE_RE.search(text):
        return Classification("anonymous", "anonymous", "", keep=False, drop_reason="anonymous")
    if _COLUMN_RE.search(title.strip()):
        return Classification("column", "column", "", keep=False, drop_reason="column")
    if _contains(text, _DIGEST_WORDS):
        return Classification("digest", "digest", "", keep=False, drop_reason="digest")
    if _contains(text, _NONMARKET_WORDS):
        return Classification("nonmarket", "nonmarket", "", keep=False, drop_reason="nonmarket")

    # 2) 地区过滤：地缘先于欧洲/其他地区，能源供给事件走商品通道
    if _contains(text, _GEOPOLITICS_WORDS):
        if _contains(text, _ENERGY_SUPPLY_WORDS):
            return Classification("commodity_oil", "commodity_oil", "commodity", keep=True, mapped_sw_l1=("石油石化",), subject_resolved=True)
        return Classification("geopolitics", "geopolitics", "", keep=False, drop_reason="geopolitics")

    # 3) 美股链主 / 科技映射（先于宽基指数）
    for key, industries, words, exclusions in _US_CHAIN_RULES:
        if _contains(text, words) and not _contains(text, exclusions):
            return Classification(key, key, "us_chain", keep=True, mapped_sw_l1=industries, subject_resolved=True)

    # 4) 日韩行业映射
    if _contains(text, _JPKR_WORDS):
        for key, industries, words in _JPKR_INDUSTRY_RULES:
            if _contains(text, words):
                return Classification(key, key, "jpkr_chain", keep=True, mapped_sw_l1=industries, subject_resolved=True)
        return Classification("jpkr_generic", "jpkr_generic", "jpkr_chain", keep=True)

    # 5) 欧洲（仅展示，权重 0）；若为能源事件则走商品通道
    if _contains(text, _EUROPE_WORDS):
        if _contains(text, _ENERGY_SUPPLY_WORDS):
            return Classification("commodity_oil", "commodity_oil", "commodity", keep=True, mapped_sw_l1=("石油石化",), subject_resolved=True)
        return Classification("europe", "europe", "europe", keep=True)

    # 6) 其他地区直接丢弃
    if _contains(text, _OTHER_REGION_WORDS):
        return Classification("other_region", "other_region", "", keep=False, drop_reason="other_region")

    # 7) 商品：具名品类才映射到证据；同期 β 仅参考，权重为 0（滞后一日不显著）
    for key, industries, words in _COMMODITY_RULES:
        if _contains(text, words):
            return Classification(key, key, "commodity", keep=True, mapped_sw_l1=industries, subject_resolved=True)

    # 8) 美股宽基指数（先于泛商品，避免“纳指期货”被判成商品）
    for key, words in _US_INDEX_RULES:
        if _contains(text, words):
            return Classification(key, key, "us_index", keep=True, subject_resolved=True)

    # 9) 未具名商品
    if _contains(text, _COMMODITY_GENERIC_WORDS):
        return Classification("commodity_generic", "commodity_generic", "commodity", keep=True)

    # 10) 政策：例行事务先丢弃，中央级 + 工具 + 行业才进入产业政策通道
    if _contains(text, _POLICY_ROUTINE_WORDS):
        return Classification("policy_routine", "policy_routine", "", keep=False, drop_reason="policy_routine")
    if _contains(text, _CENTRAL_WORDS) and _contains(text, _POLICY_TOOL_WORDS):
        industries = tuple(word for word in _POLICY_INDUSTRY_WORDS if word in text)
        return Classification(
            "industrial_policy",
            "industrial_policy",
            "industrial_policy",
            keep=True,
            mapped_sw_l1=industries,
            subject_resolved=bool(industries),
        )

    # 10) 宏观
    for key, words in _MACRO_RULES:
        if _contains(text, words):
            return Classification(key, key, "major_macro" if key == "major_macro" else "macro", keep=True,
                                  subject_resolved=key == "major_macro")

    # 11) 指数复盘 / 泛行业 / 其他
    if _contains(text, _INDEX_RECAP_WORDS):
        return Classification("index_recap", "index_recap", "", keep=True)
    if _contains(text, _SECTOR_GENERIC_WORDS):
        return Classification("sector_generic", "sector_generic", "", keep=True)
    return Classification("other", "other", "", keep=True)


def _detect_direction(text: str) -> int:
    positive = _contains(text, _DIRECTION_POSITIVE)
    negative = _contains(text, _DIRECTION_NEGATIVE)
    if positive and not negative:
        return 1
    if negative and not positive:
        return -1
    return 0


@dataclass(frozen=True)
class TriageResult:
    category: str
    channel: str
    keep: bool
    display_only: bool
    score: int
    priority: int
    direction: int
    evidence_key: str
    evidence_strength: str
    zero_reason: str
    drop_reason: str
    mapped_sw_l1: Tuple[str, ...] = field(default_factory=tuple)
    mapped_stock_codes: Tuple[str, ...] = field(default_factory=tuple)
    evidence: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "category": self.category,
            "channel": self.channel,
            "keep": self.keep,
            "display_only": self.display_only,
            "score": self.score,
            "priority": self.priority,
            "direction": self.direction,
            "evidence_key": self.evidence_key,
            "evidence_strength": self.evidence_strength,
            "zero_reason": self.zero_reason,
            "drop_reason": self.drop_reason,
            "mapped_sw_l1": list(self.mapped_sw_l1),
            "mapped_stock_codes": list(self.mapped_stock_codes),
            "evidence": dict(self.evidence),
        }


def triage_news_item(
    raw: Mapping[str, Any],
    *,
    name_matcher: Optional[AStockNameMatcher] = None,
) -> TriageResult:
    """对一条 ``no_stock`` 新闻分类并打分。"""

    title = str(raw.get("title") or "")
    text = _build_text(
        title,
        raw.get("summary"),
        raw.get("content"),
        raw.get("normalized_content"),
    )
    classification = _classify(text, title)
    evidence = EVIDENCE[classification.evidence_key]
    score = evidence.score
    direction = _detect_direction(text)

    mapped_codes: Tuple[str, ...] = ()
    if name_matcher is not None:
        mapped_codes = tuple(sorted({code for code, _ in name_matcher.find(text)}))

    priority = score if (classification.keep and classification.subject_resolved and direction != 0) else 0
    display_only = (not classification.keep) or priority == 0

    return TriageResult(
        category=classification.category,
        channel=classification.channel,
        keep=classification.keep,
        display_only=display_only,
        score=score,
        priority=priority,
        direction=direction,
        evidence_key=evidence.key,
        evidence_strength=evidence.evidence_strength,
        zero_reason=evidence.zero_reason,
        drop_reason=classification.drop_reason,
        mapped_sw_l1=classification.mapped_sw_l1,
        mapped_stock_codes=mapped_codes,
        evidence=evidence.to_dict(),
    )


def triage_many(
    items: Sequence[Mapping[str, Any]],
    *,
    name_matcher: Optional[AStockNameMatcher] = None,
) -> List[TriageResult]:
    """批量分类并按 ``priority`` / ``score`` 降序排序（只排序，不做筛选）。"""

    results = [triage_news_item(item, name_matcher=name_matcher) for item in items]
    return sorted(results, key=lambda item: (item.priority, item.score, len(item.mapped_sw_l1)), reverse=True)
