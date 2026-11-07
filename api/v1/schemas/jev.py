"""Jev daily shadow API inputs."""

from datetime import date

from pydantic import BaseModel, ConfigDict, Field


class JevEvaluateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    stock_code: str = Field(min_length=1, max_length=32, pattern=r"^[A-Za-z0-9.\-^]+$")
    as_of: date | None = None
    account_id: int | None = Field(default=None, gt=0)
    fallback_to_network: bool = False
