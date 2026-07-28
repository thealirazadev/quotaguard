"""The hot path. Async and Redis-only: a cache miss resolves the key in a threadpool.

Unknown and revoked keys are answered in-band with allowed=false, because the
gateway needs a decision on every call rather than an error branch.
"""

import logging

from fastapi import APIRouter, Depends

from app.deps import require_service_token
from app.schemas.check import CheckOut, CheckRequest
from app.services import checker, keycache

logger = logging.getLogger("quotaguard.check")

router = APIRouter(prefix="/v1", tags=["check"], dependencies=[Depends(require_service_token)])


def _refused(reason: str) -> CheckOut:
    """Unknown or revoked key: no Redis is touched and there are no layers to report."""
    return CheckOut(allowed=False, reason=reason)


@router.post("/check")
async def check(payload: CheckRequest) -> dict:
    key = await keycache.resolve(payload.api_key)
    if key is None:
        logger.info(
            "check refused",
            extra={"event": "check.unknown_key", "resource": payload.resource},
        )
        return {"data": _refused("unknown_key")}
    if key.revoked:
        logger.info(
            "check refused",
            extra={
                "event": "check.denied",
                "key_id": key.key_id,
                "resource": payload.resource,
                "deny_layer": "revoked_key",
            },
        )
        return {"data": _refused("revoked_key")}

    result = await checker.run_check(key, payload.resource, payload.cost)
    if result.soft_crossed:
        logger.info(
            "quota soft threshold crossed",
            extra={
                "event": "quota.soft_crossed",
                "key_id": key.key_id,
                "month": result.month,
            },
        )
    if not result.allowed:
        logger.info(
            "check denied",
            extra={
                "event": "check.denied",
                "key_id": key.key_id,
                "resource": payload.resource,
                "deny_layer": result.deny_layer,
            },
        )
    else:
        logger.debug(
            "check allowed",
            extra={"key_id": key.key_id, "resource": payload.resource},
        )
    return {"data": checker.to_out(key, result)}
