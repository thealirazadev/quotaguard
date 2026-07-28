"""Check request and response models.

The resource charset and the cost bounds are validated here; the plan limits were
already bounded when the plan was written.
"""

from pydantic import BaseModel, ConfigDict, Field

RESOURCE_PATTERN = r"^[a-zA-Z0-9_.:/-]{1,128}$"

# qk_ plus 43 url-safe characters; the bound only keeps absurd bodies out of hashing.
MAX_API_KEY_LENGTH = 256


class CheckRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    api_key: str = Field(min_length=1, max_length=MAX_API_KEY_LENGTH)
    resource: str = Field(pattern=RESOURCE_PATTERN)
    cost: int = Field(default=1, ge=1, le=1000)


class BurstLayerOut(BaseModel):
    limit: int
    remaining: int
    reset: int


class SustainedLayerOut(BaseModel):
    limit: int
    remaining: int
    reset: int


class QuotaLayerOut(BaseModel):
    month: str
    limit: int
    used: int
    remaining: int
    reset: int
    soft_threshold_crossed: bool


class LayersOut(BaseModel):
    burst: BurstLayerOut
    sustained: SustainedLayerOut
    quota: QuotaLayerOut


class CheckOut(BaseModel):
    """One decision. `reason` is null, burst, sustained, quota, unknown_key,
    revoked_key, or redis_unavailable; the top-level limit/remaining/reset mirror
    the layer carried by the RateLimit header."""

    allowed: bool
    reason: str | None = None
    degraded: bool = False
    limit: int | None = None
    remaining: int | None = None
    reset: int | None = None
    retry_after: int | None = None
    headers: dict[str, str] = Field(default_factory=dict)
    layers: LayersOut | None = None
