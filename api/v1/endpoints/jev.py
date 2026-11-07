"""Jev evaluations share the application's authentication and portfolio scope."""

from fastapi import APIRouter, HTTPException, Query

from api.v1.schemas.jev import JevEvaluateRequest
from src.agent.pricing.repository import PricePlanRepository
from src.core.trading_calendar import get_market_for_stock
from src.services.jev_evaluation_service import JevEvaluationService, completed_session, normalize_symbol

router = APIRouter()


@router.get("/status")
def status() -> dict:
    return JevEvaluationService().status()


@router.post("/evaluate")
def evaluate(request: JevEvaluateRequest) -> dict:
    service = JevEvaluationService()
    symbol = normalize_symbol(request.stock_code)
    if request.as_of is not None:
        try:
            completed_session(get_market_for_stock(symbol), request.as_of)
        except (ValueError, KeyError, OverflowError):
            raise HTTPException(status_code=422, detail="Invalid date or exchange calendar") from None
    plan = PricePlanRepository().get_latest_artifact(symbol)
    position = None
    if request.account_id is not None:
        from src.services.portfolio_service import PortfolioService

        try:
            snapshot = PortfolioService().get_portfolio_snapshot(account_id=request.account_id, as_of=request.as_of)
        except ValueError:
            raise HTTPException(status_code=404, detail="Account not found") from None
        for account in snapshot.get("accounts", []):
            for item in account.get("positions", []):
                if normalize_symbol(str(item.get("symbol") or "")) == symbol and float(item.get("quantity") or 0) > 0:
                    position = {"quantity": item["quantity"], "average_cost": item.get("avg_cost")}
        if position is None:
            raise HTTPException(status_code=404, detail="Position not found in this account")
    return service.evaluate(
        symbol, as_of=request.as_of, source="manual",
        source_ref=f"account:{request.account_id}" if request.account_id else None,
        price_plan=plan.model_dump(mode="json") if plan else None,
        position=position, fallback_to_network=request.fallback_to_network,
    )


@router.get("/evaluations")
def evaluations(stock_code: str | None = Query(default=None, max_length=32),
                limit: int = Query(default=50, ge=1, le=200)) -> dict:
    items = JevEvaluationService().list_recent(stock_code=stock_code, limit=limit)
    return {"items": [{key: value for key, value in item.items() if key not in {"state", "questions"}} for item in items]}


@router.get("/evaluations/{evaluation_id}")
def evaluation_detail(evaluation_id: str) -> dict:
    service = JevEvaluationService()
    item = service.repo.get(evaluation_id)
    if item is None:
        raise HTTPException(status_code=404, detail="Evaluation not found")
    return {**item, "outcomes": service.outcomes(item)}
