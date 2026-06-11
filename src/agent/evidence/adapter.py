# -*- coding: utf-8 -*-
"""Adapters that compact raw tool results into EvidenceCards."""

from __future__ import annotations

import json
from typing import Any, Dict, Iterable, List, Optional

from src.agent.evidence.freshness import default_window_for_dimension, infer_freshness, valid_until_for
from src.agent.evidence.schemas import (
    CounterEvidence,
    DataQuality,
    EvidenceCard,
    EvidenceExpiry,
    EvidenceImpact,
    EvidenceSignal,
    ExpertEvidencePacket,
    StockRef,
)
from src.agent.evidence.scoring import clamp_confidence, clamp_score_delta, strength_from_delta


_TOOL_DIMENSION = {
    "get_realtime_quote": "quote",
    "analyze_trend": "technical",
    "calculate_ma": "technical",
    "get_volume_analysis": "technical",
    "analyze_pattern": "technical_pattern",
    "analyze_price_structure": "price_structure",
    "get_capital_flow": "capital_flow",
    "get_chip_distribution": "chip",
    "get_stock_info": "fundamental",
    "search_comprehensive_intel": "news_event",
    "detect_market_regime": "regime",
    "get_sector_rankings": "sector",
    "get_market_indices": "regime",
}


def build_evidence_cards_for_stock(
    *,
    run_id: str,
    stock_code: str,
    stock_name: str = "",
    market: str = "cn",
    evidence: Dict[str, Any],
    raw_ref_prefix: str = "tool_raw",
) -> List[EvidenceCard]:
    """Build evidence cards for one stock's raw tool-evidence map."""
    stock = StockRef(code=str(stock_code), name=str(stock_name or stock_code), market=market or "cn")
    cards: List[EvidenceCard] = []
    for tool_name, raw in (evidence or {}).items():
        if not isinstance(raw, dict):
            continue
        dimension = _TOOL_DIMENSION.get(tool_name)
        if not dimension:
            continue
        card = build_evidence_card(
            run_id=run_id,
            stock=stock,
            dimension=dimension,
            tool_name=tool_name,
            raw=raw,
            raw_ref=f"{raw_ref_prefix}/{tool_name}/{stock.code}.json",
        )
        cards.append(card)
    return cards


def build_evidence_card(
    *,
    run_id: str,
    stock: StockRef,
    dimension: str,
    tool_name: str,
    raw: Dict[str, Any],
    raw_ref: str,
) -> EvidenceCard:
    """Convert one raw tool result into an EvidenceCard."""
    status = _quality_status(raw)
    as_of = _as_of(raw)
    freshness = infer_freshness(as_of, dimension)
    source_chain = raw.get("source_chain") if isinstance(raw.get("source_chain"), list) else []
    source = _source_from_chain(source_chain) or str(raw.get("source") or tool_name)
    warnings = _warnings(raw, freshness)
    missing_fields = _missing_fields_for(tool_name, raw)
    signals = _signals_for(tool_name, raw)
    signals.sort(key=lambda item: abs(item.score_delta), reverse=True)
    impact = _impact_from_signals(tool_name, status, signals, missing_fields)
    counter = _counter_evidence_for(tool_name, impact, raw)
    window, trigger = default_window_for_dimension(dimension)
    return EvidenceCard(
        card_id=f"{dimension}:{stock.code}:{as_of or 'unknown'}:{tool_name}",
        run_id=run_id,
        stock=stock,
        dimension=dimension,
        producer={"tool": tool_name, "version": "evidence-card-v1"},
        data_quality=DataQuality(
            status=status,
            as_of=as_of,
            freshness=freshness,
            source=source,
            source_chain=source_chain[:5],
            warnings=warnings,
            missing_fields=missing_fields,
        ),
        signals=signals[:5],
        impact=impact,
        counter_evidence=counter,
        expiry=EvidenceExpiry(
            valid_until=valid_until_for(as_of, dimension),
            refresh_trigger=trigger,
            window=window,
        ),
        raw_ref=raw_ref,
    )


def build_expert_packet(
    *,
    run_id: str,
    expert: str,
    dimension: str,
    cards: Iterable[EvidenceCard],
    stock: Optional[StockRef] = None,
    missing_hint: str = "",
) -> ExpertEvidencePacket:
    """Aggregate dimension cards into one compact expert packet."""
    matched = [card for card in cards if card.dimension == dimension or _packet_dimension_match(dimension, card.dimension)]
    if not matched:
        return ExpertEvidencePacket(
            packet_id=f"{expert}:{stock.code if stock else 'market'}:{run_id}",
            expert=expert,
            dimension=dimension,
            stock=stock,
            stance="invalid",
            action_bias="wait",
            confidence=0.0,
            summary=f"{dimension} 证据缺失，本轮不能作为支持证据。",
            top_risks=[f"{dimension} 证据缺失，禁止单维度确认买入。"],
            missing_evidence=[missing_hint or dimension],
        )

    ordered = sorted(matched, key=lambda card: abs(card.impact.score_delta) * card.impact.confidence, reverse=True)
    supports = [
        _card_line(card)
        for card in ordered
        if card.impact.stance == "support"
    ][:3]
    risks = [
        _card_line(card)
        for card in ordered
        if card.impact.stance in {"oppose", "wait_confirm", "invalid"}
    ][:3]
    counter = [
        item.refutation
        for card in ordered
        for item in card.counter_evidence
        if item.refutation
    ][:3]
    score = sum(card.impact.score_delta * card.impact.confidence for card in matched)
    if any(card.impact.stance == "invalid" for card in matched) and not supports:
        stance = "invalid"
        action = "wait"
    elif score >= 5:
        stance = "support"
        action = "open"
    elif score <= -5:
        stance = "oppose"
        action = "wait"
    elif risks:
        stance = "wait_confirm"
        action = "wait"
    else:
        stance = "neutral"
        action = "hold"
    confidence = clamp_confidence(sum(card.impact.confidence for card in matched) / max(len(matched), 1))
    summary = _packet_summary(dimension, stance, supports, risks or counter)
    return ExpertEvidencePacket(
        packet_id=f"{expert}:{stock.code if stock else 'market'}:{run_id}",
        expert=expert,
        dimension=dimension,
        stock=stock,
        stance=stance,
        action_bias=action,
        confidence=confidence,
        summary=summary,
        top_supports=supports,
        top_risks=list(dict.fromkeys(risks + counter))[:3],
        key_cards=[card.card_id for card in ordered[:5]],
        missing_evidence=list(dict.fromkeys(field for card in matched for field in card.data_quality.missing_fields))[:5],
        recommended_next_tools=_recommended_tools(matched),
        raw_refs=list(dict.fromkeys(card.raw_ref for card in ordered if card.raw_ref))[:5],
    )


def cards_to_json(cards: Iterable[EvidenceCard]) -> List[Dict[str, Any]]:
    """Serialize cards to JSON-safe dicts."""
    return [card.model_dump(mode="json") for card in cards]


def packets_to_json(packets: Iterable[ExpertEvidencePacket]) -> List[Dict[str, Any]]:
    """Serialize expert packets to JSON-safe dicts."""
    return [packet.model_dump(mode="json") for packet in packets]


def _quality_status(raw: Dict[str, Any]) -> str:
    status = str(raw.get("status") or "").strip().lower()
    if raw.get("timeout") is True:
        return "timeout"
    if status in {"ok", "partial", "empty", "stale", "failed", "timeout", "not_supported"}:
        return status
    if raw.get("error") or raw.get("errors"):
        return "failed"
    return "partial"


def _as_of(raw: Dict[str, Any]) -> Optional[str]:
    for key in ("latest_date", "quote_trade_date", "date", "as_of", "trade_date", "source_update"):
        value = raw.get(key)
        if value:
            return str(value)[:10]
    return None


def _source_from_chain(chain: List[Dict[str, Any]]) -> str:
    for item in reversed(chain or []):
        if isinstance(item, dict) and item.get("result") == "ok" and item.get("provider"):
            return str(item["provider"])
    for item in chain or []:
        if isinstance(item, dict) and item.get("provider"):
            return str(item["provider"])
    return ""


def _warnings(raw: Dict[str, Any], freshness: str) -> List[str]:
    warnings: List[str] = []
    if raw.get("errors"):
        warnings.append("tool_errors_present")
    if freshness == "stale":
        warnings.append("stale_data")
    if any(str(item).lower().find("fallback") >= 0 for item in raw.get("source_chain") or []):
        warnings.append("fallback_used")
    return warnings


def _missing_fields_for(tool_name: str, raw: Dict[str, Any]) -> List[str]:
    required = {
        "get_realtime_quote": ["price"],
        "analyze_trend": ["trend_status"],
        "calculate_ma": ["ma"],
        "get_volume_analysis": ["pattern"],
        "analyze_pattern": ["summary"],
        "analyze_price_structure": ["chan", "smc"],
        "get_capital_flow": ["main_net_inflow"],
        "get_chip_distribution": ["profit_ratio", "avg_cost"],
        "get_stock_info": ["name"],
        "search_comprehensive_intel": ["report"],
        "detect_market_regime": ["regime", "risk_level"],
    }.get(tool_name, [])
    return [field for field in required if raw.get(field) in (None, "", [], {})]


def _signals_for(tool_name: str, raw: Dict[str, Any]) -> List[EvidenceSignal]:
    if tool_name == "get_realtime_quote":
        return _quote_signals(raw)
    if tool_name == "analyze_trend":
        return _trend_signals(raw)
    if tool_name == "calculate_ma":
        return _ma_signals(raw)
    if tool_name == "get_volume_analysis":
        return _volume_signals(raw)
    if tool_name == "analyze_pattern":
        return _pattern_signals(raw)
    if tool_name == "analyze_price_structure":
        return _structure_signals(raw)
    if tool_name == "get_capital_flow":
        return _capital_signals(raw)
    if tool_name == "get_chip_distribution":
        return _chip_signals(raw)
    if tool_name == "get_stock_info":
        return _fundamental_signals(raw)
    if tool_name == "search_comprehensive_intel":
        return _news_signals(raw)
    if tool_name == "detect_market_regime":
        return _regime_signals(raw)
    return []


def _quote_signals(raw: Dict[str, Any]) -> List[EvidenceSignal]:
    change = _num(raw.get("change_pct"))
    turnover = _num(raw.get("turnover_rate"))
    signals = []
    if change is not None:
        delta = clamp_score_delta(change)
        signals.append(_signal("change_pct", change, "%", delta, f"涨跌幅 {change}%。"))
    if turnover is not None:
        delta = 4 if turnover >= 5 else (2 if turnover >= 2 else 0)
        signals.append(_signal("turnover_rate", turnover, "%", delta, f"换手率 {turnover}%。"))
    return signals


def _trend_signals(raw: Dict[str, Any]) -> List[EvidenceSignal]:
    status = str(raw.get("trend_status") or "")
    bias = _num(raw.get("bias_ma5"))
    delta = 0
    if "多头" in status:
        delta += 8
    if "空头" in status:
        delta -= 10
    if bias is not None and bias > 5:
        delta -= 6
    return [_signal("trend_status", status, None, delta, f"趋势状态：{status or '未知'}。")]


def _ma_signals(raw: Dict[str, Any]) -> List[EvidenceSignal]:
    alignment = str(raw.get("ma_alignment") or "")
    above = _num(raw.get("above_ma_count"))
    total = _num(raw.get("total_ma_count"))
    delta = 0
    if "多头" in alignment:
        delta += 8
    elif "空头" in alignment:
        delta -= 8
    elif above is not None and total:
        delta += int((above / total - 0.5) * 10)
    return [_signal("ma_alignment", alignment, None, delta, f"均线状态：{alignment or '未知'}。")]


def _volume_signals(raw: Dict[str, Any]) -> List[EvidenceSignal]:
    pattern = str(raw.get("pattern") or "")
    trend = str(raw.get("volume_trend") or "")
    ratio = _num(raw.get("volume_ratio_vs_20d"))
    delta = 0
    if "放量" in trend or (ratio is not None and ratio >= 1.3):
        delta += 5
    if "缩量" in trend or (ratio is not None and ratio < 0.8):
        delta -= 3
    return [_signal("volume_pattern", ratio, None, delta, f"量价：{pattern or trend or '未知'}。")]


def _pattern_signals(raw: Dict[str, Any]) -> List[EvidenceSignal]:
    summary = str(raw.get("summary") or "")
    patterns = raw.get("patterns") if isinstance(raw.get("patterns"), list) else []
    delta = 0
    text = json.dumps(patterns, ensure_ascii=False, default=str)
    if "bullish" in text or "看涨" in summary or "双底" in summary or "突破" in summary:
        delta += 8
    if "bearish" in text or "看跌" in summary or "黄昏" in summary or "流星" in summary:
        delta -= 8
    if not patterns:
        delta = 0
    return [_signal("kline_pattern", summary or None, None, delta, f"K线形态：{summary or '未发现明显形态'}。")]


def _structure_signals(raw: Dict[str, Any]) -> List[EvidenceSignal]:
    smc = raw.get("smc") if isinstance(raw.get("smc"), dict) else {}
    chan = raw.get("chan") if isinstance(raw.get("chan"), dict) else {}
    text = _short_json({"chan": chan, "smc": smc})
    delta = 0
    if "bullish" in text:
        delta += 6
    if "bearish" in text:
        delta -= 6
    return [_signal("price_structure", None, None, delta, f"结构摘要：{text}")]


def _capital_signals(raw: Dict[str, Any]) -> List[EvidenceSignal]:
    latest = _num(raw.get("main_net_inflow"))
    inflow_5d = _num(raw.get("main_inflow_5d") if raw.get("main_inflow_5d") is not None else raw.get("inflow_5d"))
    signals = []
    if latest is not None:
        delta = 8 if latest > 0 else -8
        signals.append(_signal("main_net_inflow", latest, "CNY", delta, "主力净流入为正。" if latest > 0 else "主力净流入为负。"))
    if inflow_5d is not None:
        delta = 6 if inflow_5d > 0 else -6
        signals.append(_signal("main_inflow_5d", inflow_5d, "CNY", delta, "5日主力净流入为正。" if inflow_5d > 0 else "5日主力净流入为负。"))
    return signals


def _chip_signals(raw: Dict[str, Any]) -> List[EvidenceSignal]:
    profit = _num(raw.get("profit_ratio"))
    avg_cost = _num(raw.get("avg_cost"))
    signals = []
    if profit is not None:
        delta = -5 if profit >= 0.85 else (3 if 0.35 <= profit <= 0.75 else 0)
        signals.append(_signal("profit_ratio", profit, "ratio", delta, f"获利盘比例 {profit}。"))
    if avg_cost is not None:
        signals.append(_signal("avg_cost", avg_cost, "CNY", 0, f"平均成本 {avg_cost}。"))
    return signals


def _fundamental_signals(raw: Dict[str, Any]) -> List[EvidenceSignal]:
    status = str(raw.get("status") or "partial")
    boards = raw.get("belong_boards") or raw.get("boards") or []
    delta = -6 if status in {"failed", "timeout"} else 1
    return [_signal("fundamental_context", len(boards) if isinstance(boards, list) else None, None, delta, "基本面信息已获取。" if delta > 0 else "基本面信息不可用。")]


def _news_signals(raw: Dict[str, Any]) -> List[EvidenceSignal]:
    text = str(raw.get("report") or raw.get("summary") or "")
    lowered = text.lower()
    delta = 0
    if any(word in text for word in ("利空", "减持", "监管", "处罚", "亏损")):
        delta -= 8
    elif any(word in text for word in ("利好", "订单", "增长", "中标")):
        delta += 5
    elif "no major" in lowered or "无重大利空" in text:
        delta += 1
    return [_signal("news_event_summary", None, None, delta, text[:180] or "消息面无有效摘要。")]


def _regime_signals(raw: Dict[str, Any]) -> List[EvidenceSignal]:
    regime = str(raw.get("regime") or "unknown")
    risk = str(raw.get("risk_level") or "unknown")
    delta = 0
    if regime in {"risk_off", "panic"} or risk in {"high", "extreme", "critical"}:
        delta -= 12
    elif regime in {"trending_up", "risk_on"}:
        delta += 6
    return [_signal("market_regime", regime, None, delta, f"市场状态 {regime}，风险等级 {risk}。")]


def _signal(name: str, value: Any, unit: Optional[str], delta: float, interpretation: str) -> EvidenceSignal:
    delta = clamp_score_delta(delta)
    if delta > 0:
        direction = "positive"
    elif delta < 0:
        direction = "negative"
    else:
        direction = "neutral"
    return EvidenceSignal(
        name=name,
        value=value,
        unit=unit,
        direction=direction,
        strength=strength_from_delta(delta),
        score_delta=delta,
        interpretation=interpretation,
    )


def _impact_from_signals(tool_name: str, status: str, signals: List[EvidenceSignal], missing_fields: List[str]) -> EvidenceImpact:
    if status in {"failed", "timeout", "empty", "not_supported"}:
        return EvidenceImpact(
            stance="invalid",
            action_bias="wait",
            confidence=0.0,
            score_delta=0.0,
            reason=f"{tool_name} 数据不可用，不能作为支持证据。",
        )
    score = clamp_score_delta(sum(signal.score_delta for signal in signals))
    if score >= 5:
        stance = "support"
        action = "open"
    elif score <= -5:
        stance = "oppose"
        action = "wait"
    elif missing_fields:
        stance = "wait_confirm"
        action = "wait"
    else:
        stance = "neutral"
        action = "hold"
    confidence = 0.45 if missing_fields else 0.65
    if status == "partial":
        confidence -= 0.15
    return EvidenceImpact(
        stance=stance,
        action_bias=action,
        confidence=clamp_confidence(confidence),
        score_delta=score,
        reason=_impact_reason(tool_name, stance),
    )


def _counter_evidence_for(tool_name: str, impact: EvidenceImpact, raw: Dict[str, Any]) -> List[CounterEvidence]:
    if impact.stance not in {"oppose", "invalid", "wait_confirm"}:
        return []
    refuted = {
        "get_capital_flow": "技术面突破可立即买入",
        "get_chip_distribution": "筹码结构支持立即入场",
        "search_comprehensive_intel": "消息面不存在风险",
        "detect_market_regime": "市场环境允许提高仓位",
    }.get(tool_name, "当前交易假设可以直接执行")
    return [
        CounterEvidence(
            refuted_claim=refuted,
            refutation=impact.reason or str(raw.get("error") or raw.get("errors") or "证据不足"),
            severity="medium" if impact.stance != "invalid" else "high",
        )
    ]


def _impact_reason(tool_name: str, stance: str) -> str:
    if stance == "support":
        return f"{tool_name} 提供正向有效证据。"
    if stance == "oppose":
        return f"{tool_name} 提供反向证据，应降低动作强度。"
    if stance == "wait_confirm":
        return f"{tool_name} 证据不完整，需要等待确认。"
    if stance == "invalid":
        return f"{tool_name} 数据无效。"
    return f"{tool_name} 未形成明确方向。"


def _packet_dimension_match(packet_dimension: str, card_dimension: str) -> bool:
    aliases = {
        "capital_chip": {"capital_flow", "chip"},
        "news_sentiment": {"news_event", "sentiment"},
        "market_regime": {"regime", "sector"},
        "portfolio_risk": {"risk", "account_fit"},
    }
    return card_dimension in aliases.get(packet_dimension, set())


def _card_line(card: EvidenceCard) -> str:
    signal = card.signals[0].interpretation if card.signals else card.impact.reason
    return f"{card.stock.code} {card.stock.name}: {signal}"


def _packet_summary(dimension: str, stance: str, supports: List[str], risks: List[str]) -> str:
    if stance == "support":
        return f"{dimension} 维度有正向证据：{supports[0] if supports else '支持'}"
    if stance == "oppose":
        return f"{dimension} 维度存在反证：{risks[0] if risks else '反对'}"
    if stance == "invalid":
        return f"{dimension} 维度证据无效或缺失。"
    if stance == "wait_confirm":
        return f"{dimension} 维度需要等待确认：{risks[0] if risks else '证据不完整'}"
    return f"{dimension} 维度未形成明确方向。"


def _recommended_tools(cards: List[EvidenceCard]) -> List[Dict[str, Any]]:
    tools = []
    for card in cards:
        tool = str(card.producer.get("tool") or "")
        if card.data_quality.status in {"failed", "timeout", "empty", "stale"} and tool:
            tools.append({"tool": tool, "reason": "刷新缺失或过期证据"})
    return tools[:3]


def _num(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(str(value).replace(",", "").replace("%", "").strip())
    except Exception:
        return None


def _short_json(value: Any, max_chars: int = 180) -> str:
    text = json.dumps(value, ensure_ascii=False, default=str)
    return text if len(text) <= max_chars else text[:max_chars] + "..."
