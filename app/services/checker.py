"""The admission call: marshal ARGV, run the atomic script, decode its tuple.

Nothing here decides anything. The whole admission decision lives in
app/lua/check.lua and happens in one round trip, so this module only converts
between plan limits and the script's integer contract.
"""

import logging
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from app import redis_client
from app.errors import InternalError
from app.schemas.check import (
    BurstLayerOut,
    CheckOut,
    LayersOut,
    QuotaLayerOut,
    SustainedLayerOut,
)
from app.services.keycache import ResolvedKey

logger = logging.getLogger("quotaguard.checker")

SCRIPT_NAME = "check"
UTOK = 1_000_000
TUPLE_LENGTH = 15


@dataclass(frozen=True)
class ScriptResult:
    """The 15-element return tuple of check.lua, named."""

    allowed: bool
    deny_layer: str
    burst_limit: int
    burst_remaining: int
    burst_reset: int
    sustained_limit: int
    sustained_remaining: int
    sustained_reset: int
    quota_limit: int
    quota_used: int
    quota_remaining: int
    quota_reset: int
    soft_crossed: bool
    month: str
    retry_after: int


def _text(value: Any) -> str:
    return value.decode("utf-8") if isinstance(value, bytes) else str(value)


def decode(raw: Sequence[Any]) -> ScriptResult:
    if raw is None or len(raw) != TUPLE_LENGTH:
        logger.error("admission script returned %s elements", "no" if raw is None else len(raw))
        raise InternalError("The admission decision could not be read.")
    return ScriptResult(
        allowed=bool(raw[0]),
        deny_layer=_text(raw[1]),
        burst_limit=int(raw[2]),
        burst_remaining=int(raw[3]),
        burst_reset=int(raw[4]),
        sustained_limit=int(raw[5]),
        sustained_remaining=int(raw[6]),
        sustained_reset=int(raw[7]),
        quota_limit=int(raw[8]),
        quota_used=int(raw[9]),
        quota_remaining=int(raw[10]),
        quota_reset=int(raw[11]),
        soft_crossed=bool(raw[12]),
        month=_text(raw[13]),
        retry_after=int(raw[14]),
    )


def script_keys(key_id: str, resource: str) -> list[str]:
    """The quota and flag keys are prefixes: only the script knows the month."""
    return [
        f"qg:b:{key_id}:{resource}",
        f"qg:s:{key_id}:{resource}",
        f"qg:q:{key_id}:",
        f"qg:f:{key_id}:",
    ]


def script_argv(key: ResolvedKey, cost: int, request_id: str) -> list[Any]:
    soft = key.monthly_quota * key.quota_soft_pct // 100 if key.quota_soft_pct > 0 else 0
    return [
        key.burst_capacity * UTOK,
        key.burst_refill_per_sec * UTOK,
        key.sustained_limit,
        key.sustained_window_seconds * UTOK,
        key.monthly_quota,
        soft,
        cost,
        request_id,
    ]


def to_out(result: ScriptResult) -> CheckOut:
    return CheckOut(
        allowed=result.allowed,
        reason=result.deny_layer or None,
        degraded=False,
        retry_after=result.retry_after if not result.allowed else None,
        layers=LayersOut(
            burst=BurstLayerOut(
                limit=result.burst_limit,
                remaining=result.burst_remaining,
                reset=result.burst_reset,
            ),
            sustained=SustainedLayerOut(
                limit=result.sustained_limit,
                remaining=result.sustained_remaining,
                reset=result.sustained_reset,
            ),
            quota=QuotaLayerOut(
                month=result.month,
                limit=result.quota_limit,
                used=result.quota_used,
                remaining=result.quota_remaining,
                reset=result.quota_reset,
                soft_threshold_crossed=result.soft_crossed,
            ),
        ),
    )


async def run_check(key: ResolvedKey, resource: str, cost: int) -> ScriptResult:
    """One EVALSHA, one decision. The request id only makes the window member unique."""
    raw = await redis_client.get_registry().run(
        SCRIPT_NAME,
        script_keys(key.key_id, resource),
        script_argv(key, cost, uuid.uuid4().hex),
    )
    return decode(raw)
