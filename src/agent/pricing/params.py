from __future__ import annotations

from decimal import Decimal

from pydantic import ConfigDict, BaseModel, Field


class PricePlanParams(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    version: str
    algorithm_version: str
    daily_evaluation_bars: int = Field(gt=0)
    daily_warmup_bars: int = Field(ge=0)
    weekly_evaluation_bars: int = Field(gt=0)
    weekly_warmup_bars: int = Field(ge=0)
    atr_period: int = Field(gt=1)
    cluster_gap_atr: Decimal = Field(gt=0)
    max_cluster_width_atr: Decimal = Field(gt=0)
    zone_padding_atr: Decimal = Field(gt=0)
    max_zone_width_atr: Decimal = Field(gt=0)
    break_confirm_bars: int = Field(gt=0)
    break_buffer_atr: Decimal = Field(gt=0)
    reaction_touch_atr: Decimal = Field(gt=0)
    reaction_horizon_bars: int = Field(gt=0)
    reaction_move_atr: Decimal = Field(gt=0)
    reaction_min_gap_bars: int = Field(gt=0)
    chip_anchor_max_age_bars: int = Field(gt=0)
    event_anchor_max_age_bars: int = Field(gt=0)
    structure_anchor_max_age_bars: int = Field(gt=0)
    stop_buffer_atr: Decimal = Field(gt=0)
    max_stop_distance_atr: Decimal = Field(gt=0)
    min_target_gap_atr: Decimal = Field(gt=0)
    second_target_gap_atr: Decimal = Field(gt=0)
    min_reference_net_rr_1: Decimal = Field(gt=0)
    reference_buy_cost_bps: Decimal = Field(ge=0)
    reference_buy_slippage_bps: Decimal = Field(ge=0)
    reference_sell_cost_bps: Decimal = Field(ge=0)
    reference_sell_slippage_bps: Decimal = Field(ge=0)
    near_distance_atr: Decimal = Field(gt=0)
    medium_distance_atr: Decimal = Field(gt=0)
    confirmation_valid_trading_days: int = Field(gt=0)
    order_valid_trading_days: int = Field(gt=0)
    target_1_exit_ratio: Decimal = Field(gt=0, le=1)
    confirmation_after_touch_bars: int = Field(gt=0)
    confirmation_volume_ma_bars: int = Field(gt=0)
    hammer_lower_body_ratio: Decimal = Field(gt=0)
    hammer_upper_body_ratio: Decimal = Field(ge=0)
    hammer_close_location_min: Decimal = Field(gt=0, le=1)
    breakdown_buffer_atr: Decimal = Field(gt=0)
    breakdown_volume_ratio: Decimal = Field(gt=0)
    contraction_volume_ratio: Decimal = Field(gt=0)
    expansion_volume_ratio: Decimal = Field(gt=0)


PRICE_PLAN_PARAMS_V1 = PricePlanParams(
    version="price-plan-params.v1",
    algorithm_version="deterministic-price-plan.v1",
    daily_evaluation_bars=520,
    daily_warmup_bars=200,
    weekly_evaluation_bars=60,
    weekly_warmup_bars=40,
    atr_period=14,
    cluster_gap_atr=Decimal("0.35"),
    max_cluster_width_atr=Decimal("0.70"),
    zone_padding_atr=Decimal("0.10"),
    max_zone_width_atr=Decimal("1.00"),
    break_confirm_bars=2,
    break_buffer_atr=Decimal("0.20"),
    reaction_touch_atr=Decimal("0.20"),
    reaction_horizon_bars=10,
    reaction_move_atr=Decimal("0.80"),
    reaction_min_gap_bars=8,
    chip_anchor_max_age_bars=20,
    event_anchor_max_age_bars=120,
    structure_anchor_max_age_bars=120,
    stop_buffer_atr=Decimal("0.25"),
    max_stop_distance_atr=Decimal("2.50"),
    min_target_gap_atr=Decimal("0.50"),
    second_target_gap_atr=Decimal("1.00"),
    min_reference_net_rr_1=Decimal("1.20"),
    reference_buy_cost_bps=Decimal("8"),
    reference_buy_slippage_bps=Decimal("5"),
    reference_sell_cost_bps=Decimal("8"),
    reference_sell_slippage_bps=Decimal("5"),
    near_distance_atr=Decimal("1.0"),
    medium_distance_atr=Decimal("2.5"),
    confirmation_valid_trading_days=5,
    order_valid_trading_days=2,
    target_1_exit_ratio=Decimal("0.50"),
    confirmation_after_touch_bars=2,
    confirmation_volume_ma_bars=5,
    hammer_lower_body_ratio=Decimal("2.0"),
    hammer_upper_body_ratio=Decimal("1.0"),
    hammer_close_location_min=Decimal("0.60"),
    breakdown_buffer_atr=Decimal("0.20"),
    breakdown_volume_ratio=Decimal("1.50"),
    contraction_volume_ratio=Decimal("0.80"),
    expansion_volume_ratio=Decimal("1.20"),
)


PARAMS_REGISTRY = {PRICE_PLAN_PARAMS_V1.version: PRICE_PLAN_PARAMS_V1}


def get_price_plan_params(version: str) -> PricePlanParams | None:
    return PARAMS_REGISTRY.get(str(version or "").strip())
