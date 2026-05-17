# -*- coding: utf-8 -*-
"""Hard exclusion rules for L1 stock candidate pools."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
import os
from typing import Any, Dict, Iterable, List, Optional, Tuple

from src.data.stock_index_loader import get_index_stock_name
from src.data.stock_mapping import STOCK_NAME_MAP, is_meaningful_stock_name


@dataclass(frozen=True)
class CandidateExclusionPolicy:
    """Configurable hard-exclusion policy for candidate discovery."""

    blacklist_codes: frozenset[str] = field(default_factory=frozenset)
    min_avg_amount: float = 0.0
    min_listing_days: int = 0
    enforce_name_code_match: bool = True


def resolve_candidate_exclusion_policy() -> CandidateExclusionPolicy:
    """Resolve candidate hard-exclusion policy from runtime config."""
    try:
        from src.config import get_config

        config = get_config()
        raw_codes = _env_list("AGENT_CANDIDATE_BLACKLIST_CODES", getattr(config, "agent_candidate_blacklist_codes", []))
        min_avg_amount = _env_float("AGENT_CANDIDATE_MIN_AVG_AMOUNT", getattr(config, "agent_candidate_min_avg_amount", 0.0))
        min_listing_days = _env_int("AGENT_CANDIDATE_MIN_LISTING_DAYS", getattr(config, "agent_candidate_min_listing_days", 0))
        enforce_name_code_match = _env_bool(
            "AGENT_CANDIDATE_ENFORCE_NAME_CODE_MATCH",
            getattr(config, "agent_candidate_enforce_name_code_match", True),
        )
    except Exception:
        raw_codes = _env_list("AGENT_CANDIDATE_BLACKLIST_CODES", [])
        min_avg_amount = _env_float("AGENT_CANDIDATE_MIN_AVG_AMOUNT", 0.0)
        min_listing_days = _env_int("AGENT_CANDIDATE_MIN_LISTING_DAYS", 0)
        enforce_name_code_match = _env_bool("AGENT_CANDIDATE_ENFORCE_NAME_CODE_MATCH", True)
    return CandidateExclusionPolicy(
        blacklist_codes=frozenset(str(code).strip() for code in raw_codes if str(code).strip()),
        min_avg_amount=max(0.0, min_avg_amount),
        min_listing_days=max(0, min_listing_days),
        enforce_name_code_match=enforce_name_code_match,
    )


def apply_hard_exclusion(
    candidates: Iterable[Dict[str, Any]],
    *,
    policy: Optional[CandidateExclusionPolicy] = None,
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """Filter candidates and return diagnostics for Trace display."""
    effective_policy = policy or resolve_candidate_exclusion_policy()
    kept: List[Dict[str, Any]] = []
    excluded: List[Dict[str, Any]] = []
    reason_counts: Counter[str] = Counter()

    for item in candidates:
        reason = evaluate_hard_exclusion(item, effective_policy)
        if reason:
            code = str(item.get("code") or item.get("stock_code") or "").strip()
            name = str(item.get("name") or item.get("stock_name") or "").strip()
            reason_counts[reason] += 1
            excluded.append({
                "code": code,
                "name": name,
                "reason": reason,
                "source": item.get("source"),
            })
            continue
        kept.append(item)

    diagnostics = {
        "excluded_count": len(excluded),
        "reason_counts": dict(reason_counts),
        "examples": excluded[:8],
        "policy": {
            "blacklist_count": len(effective_policy.blacklist_codes),
            "min_avg_amount": effective_policy.min_avg_amount,
            "min_listing_days": effective_policy.min_listing_days,
            "enforce_name_code_match": effective_policy.enforce_name_code_match,
        },
    }
    return kept, diagnostics


def evaluate_hard_exclusion(item: Dict[str, Any], policy: CandidateExclusionPolicy) -> Optional[str]:
    """Return a hard-exclusion reason, or None if the candidate can remain."""
    code = str(item.get("code") or item.get("stock_code") or "").strip()
    name = str(item.get("name") or item.get("stock_name") or "").strip()
    if not code:
        return "missing_code"
    if code in policy.blacklist_codes:
        return "blacklisted"
    if _is_st_name(name) or _truthy_field(item, ("is_st", "st", "special_treatment")):
        return "st_or_special_treatment"
    if _truthy_field(item, ("is_suspended", "suspended", "halted")) or _text_field_contains(
        item,
        ("trade_status", "status", "交易状态"),
        ("停牌", "暂停交易", "suspended", "halted"),
    ):
        return "suspended"
    if _truthy_field(item, ("delist_risk", "is_delisting", "退市风险")) or _text_field_contains(
        item,
        ("risk_label", "name", "stock_name"),
        ("退市", "退", "delist"),
    ):
        return "delist_risk"
    if _truthy_field(item, ("one_word_limit", "is_one_word_limit", "limit_locked", "untradable")):
        return "untradable_limit_locked"
    if policy.min_listing_days > 0:
        listing_days = _first_number(item, ("listing_days", "listed_days", "ipo_days"))
        if listing_days is not None and listing_days < policy.min_listing_days:
            return "new_listing_risk"
    if policy.min_avg_amount > 0:
        amount = _first_number(item, ("avg_amount_20d", "amount_20d_avg", "avg_turnover", "amount", "turnover"))
        if amount is not None and amount < policy.min_avg_amount:
            return "insufficient_liquidity"
    if policy.enforce_name_code_match and _has_name_code_mismatch(code, name):
        return "name_code_mismatch"
    return None


def _env_list(name: str, default: Any) -> List[str]:
    raw = os.getenv(name)
    if raw is not None:
        return [item.strip() for item in raw.split(",") if item.strip()]
    if isinstance(default, str):
        return [item.strip() for item in default.split(",") if item.strip()]
    return [str(item).strip() for item in (default or []) if str(item).strip()]


def _env_float(name: str, default: Any) -> float:
    raw = os.getenv(name)
    try:
        return float(raw if raw not in {None, ""} else default or 0.0)
    except Exception:
        return float(default or 0.0)


def _env_int(name: str, default: Any) -> int:
    raw = os.getenv(name)
    try:
        return int(raw if raw not in {None, ""} else default or 0)
    except Exception:
        return int(default or 0)


def _env_bool(name: str, default: Any) -> bool:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return bool(default)
    return raw.strip().lower() not in {"0", "false", "no", "off"}


def _is_st_name(name: str) -> bool:
    text = str(name or "").strip().upper()
    return text.startswith("*ST") or text.startswith("ST") or " ST" in text


def _truthy_field(item: Dict[str, Any], keys: Iterable[str]) -> bool:
    for key in keys:
        value = item.get(key)
        if isinstance(value, bool):
            if value:
                return True
            continue
        if isinstance(value, (int, float)):
            if value != 0:
                return True
            continue
        text = str(value or "").strip().lower()
        if text in {"1", "true", "yes", "y", "是", "有", "风险"}:
            return True
    return False


def _text_field_contains(item: Dict[str, Any], keys: Iterable[str], needles: Iterable[str]) -> bool:
    lowered_needles = [needle.lower() for needle in needles]
    for key in keys:
        text = str(item.get(key) or "").strip().lower()
        if text and any(needle in text for needle in lowered_needles):
            return True
    return False


def _first_number(item: Dict[str, Any], keys: Iterable[str]) -> Optional[float]:
    metrics = item.get("metrics") if isinstance(item.get("metrics"), dict) else {}
    for key in keys:
        value = item.get(key, metrics.get(key))
        if value is None:
            continue
        try:
            return float(value)
        except Exception:
            continue
    return None


def _has_name_code_mismatch(code: str, name: str) -> bool:
    if not is_meaningful_stock_name(name, code):
        return False
    expected = STOCK_NAME_MAP.get(code) or get_index_stock_name(code)
    if not is_meaningful_stock_name(expected, code):
        return False
    return str(expected).strip() != str(name).strip()
