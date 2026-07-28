"""Api key request and response models. The secret appears only in KeyIssuedOut."""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_serializer

from app.models import to_iso_utc
from app.schemas.plans import (
    BurstCapacity,
    BurstRefillPerSec,
    MonthlyQuota,
    SustainedLimit,
    SustainedWindowSeconds,
)


class KeyIssueRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    plan: str
    name: str = Field(min_length=1, max_length=128)


class KeyUpdateRequest(BaseModel):
    """Rename and set or clear overrides; an explicit null clears one override."""

    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=128)
    override_burst_capacity: BurstCapacity | None = None
    override_burst_refill_per_sec: BurstRefillPerSec | None = None
    override_sustained_limit: SustainedLimit | None = None
    override_sustained_window_seconds: SustainedWindowSeconds | None = None
    override_monthly_quota: MonthlyQuota | None = None


class KeyIssuedOut(BaseModel):
    key_id: str
    name: str
    plan: str
    api_key: str
    key_prefix: str
    created_at: datetime

    @field_serializer("created_at")
    def _serialize_created_at(self, value: datetime) -> str:
        return to_iso_utc(value)


class KeyOut(BaseModel):
    key_id: str
    key_prefix: str
    name: str
    plan: str
    overrides: dict[str, int]
    revoked_at: datetime | None
    created_at: datetime

    @field_serializer("revoked_at", "created_at")
    def _serialize_timestamps(self, value: datetime | None) -> str | None:
        return to_iso_utc(value) if value is not None else None


class KeyDetailOut(KeyOut):
    effective_limits: dict[str, int]


class KeyListOut(BaseModel):
    keys: list[KeyOut]
    total: int


class KeyRevokedOut(BaseModel):
    key_id: str
    revoked_at: datetime

    @field_serializer("revoked_at")
    def _serialize_revoked_at(self, value: datetime) -> str:
        return to_iso_utc(value)
