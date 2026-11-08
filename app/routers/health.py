"""Liveness endpoint. Always HTTP 200 so a dependency outage never removes the process."""

from fastapi import APIRouter
from redis.exceptions import RedisError

from app import redis_client

router = APIRouter(tags=["health"])


@router.get("/health")
async def health() -> dict[str, str]:
    # Reported, never fatal: the check path has its own per-plan policy for an outage.
    try:
        await redis_client.get_client().ping()
        redis_status = "ok"
    except (RedisError, OSError):
        redis_status = "down"
    return {"status": "ok", "redis": redis_status}
