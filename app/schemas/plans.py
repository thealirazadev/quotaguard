"""Plan request and response models. The numeric bounds are load-bearing.

They keep every intermediate in the Lua admission arithmetic well below 2^53
(see docs/architecture.md), so they are enforced on every write.
"""

from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_serializer, field_validator

from app.models import to_iso_utc

SLUG_PATTERN = r"^[a-z0-9-]{1,64}$"

BurstCapacity = Annotated[int, Field(ge=1, le=1_000_000)]
BurstRefillPerSec = Annotated[int, Field(ge=1, le=100_000)]
SustainedLimit = Annotated[int, Field(ge=1, le=1_000_000)]
SustainedWindowSeconds = Annotated[int, Field(ge=1, le=86_400)]
MonthlyQuota = Annotated[int, Field(ge=1, le=1_000_000_000_000)]
QuotaSoftPct = Annotated[int, Field(ge=0, le=100)]
WebhookUrl = Annotated[str, Field(min_length=1, max_length=2048)]
RedisDownPolicy = Literal["fail_open", "fail_closed"]


def _validate_webhook_url(value: str | None) -> str | None:
    if value is not None and not value.startswith(("http://", "https://")):
        raise ValueError("must be an http or https URL")
    return value


class PlanCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    slug: str = Field(pattern=SLUG_PATTERN)
    name: str = Field(min_length=1, max_length=128)
    burst_capacity: BurstCapacity
    burst_refill_per_sec: BurstRefillPerSec
    sustained_limit: SustainedLimit
    sustained_window_seconds: SustainedWindowSeconds
    monthly_quota: MonthlyQuota
    quota_soft_pct: QuotaSoftPct = 0
    webhook_url: WebhookUrl | None = None
    redis_down_policy: RedisDownPolicy = "fail_open"

    _check_webhook_url = field_validator("webhook_url")(_validate_webhook_url)


class PlanUpdateRequest(BaseModel):
    """Any subset of the mutable fields; slug is immutable."""

    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=128)
    burst_capacity: BurstCapacity | None = None
    burst_refill_per_sec: BurstRefillPerSec | None = None
    sustained_limit: SustainedLimit | None = None
    sustained_window_seconds: SustainedWindowSeconds | None = None
    monthly_quota: MonthlyQuota | None = None
    quota_soft_pct: QuotaSoftPct | None = None
    webhook_url: WebhookUrl | None = None
    redis_down_policy: RedisDownPolicy | None = None

    _check_webhook_url = field_validator("webhook_url")(_validate_webhook_url)


class PlanOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    slug: str
    name: str
    burst_capacity: int
    burst_refill_per_sec: int
    sustained_limit: int
    sustained_window_seconds: int
    monthly_quota: int
    quota_soft_pct: int
    webhook_url: str | None
    redis_down_policy: str
    created_at: datetime
    updated_at: datetime

    @field_serializer("created_at", "updated_at")
    def _serialize_timestamps(self, value: datetime) -> str:
        return to_iso_utc(value)


class PlanDetailOut(PlanOut):
    keys_count: int


class PlanListOut(BaseModel):
    plans: list[PlanOut]
    total: int
