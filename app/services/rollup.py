"""Monthly quota rollup: scan Redis counters, upsert to SQLite idempotently."""

import logging
import uuid
from datetime import UTC, datetime

from redis.exceptions import RedisError
from sqlalchemy import and_, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.db import SessionLocal
from app.models import ApiKey, Rollup, utcnow
from app.redis_client import get_pool

logger = logging.getLogger("quotaguard.rollup")

LOCK_KEY = "qg:lock:rollup"
LOCK_TIMEOUT_MS = 120_000  # 120 seconds
QUOTA_PATTERN = "qg:q:*"
SCAN_COUNT = 100


class LockNotAcquired(Exception):
    """Raised when the advisory lock cannot be acquired."""

    pass


def acquire_lock(pool) -> str:
    """Acquire the advisory lock, returning the token. Raises LockNotAcquired on failure."""
    token = str(uuid.uuid4())
    try:
        result = pool.execute_command("SET", LOCK_KEY, token, "NX", "PX", LOCK_TIMEOUT_MS)
        if result:
            return token
        raise LockNotAcquired("Another rollup holds the lock")
    except RedisError as exc:
        raise LockNotAcquired(f"Redis error: {exc.__class__.__name__}") from exc


def release_lock(pool, token: str) -> None:
    """Release the lock if we still hold it."""
    try:
        # Try to use the unlock.lua script if available; fall back to a simple DELETE.
        # This ensures we only delete if the token matches.
        registry = getattr(pool, "_script_registry", None)
        if registry and "unlock" in registry.scripts:
            registry.scripts["unlock"](keys=[LOCK_KEY], args=[token])
        else:
            # Fallback: just check and delete. Not atomic, but safe for our use.
            if pool.execute_command("GET", LOCK_KEY) == token:
                pool.execute_command("DEL", LOCK_KEY)
    except Exception as exc:
        logger.warning("failed to release rollup lock: %s", exc.__class__.__name__)


def scan_quota_keys(pool) -> list[tuple[str, int]]:
    """SCAN qg:q:* and return [(key, value), ...] of all quota counters."""
    results = []
    cursor = 0

    try:
        while True:
            cursor, keys = pool.execute_command("SCAN", cursor, "MATCH", QUOTA_PATTERN, "COUNT", SCAN_COUNT)
            for key in keys:
                value = pool.execute_command("GET", key)
                if value is not None:
                    results.append((key.decode() if isinstance(key, bytes) else key, int(value)))
            if cursor == 0:
                break
    except RedisError as exc:
        logger.exception("scan failed: %s", exc.__class__.__name__)
        raise

    return results


def parse_quota_key(key: str) -> tuple[str, str] | None:
    """Parse qg:q:k_abc123:2026-07 -> (key_id, month). Returns None on parse failure."""
    parts = key.split(":")
    if len(parts) == 4 and parts[0] == "qg" and parts[1] == "q":
        return (parts[2], parts[3])
    return None


def upsert_rollup(
    db: Session, api_key_id: int, month: str, used: int, quota_limit: int
) -> None:
    """Idempotent upsert: only raise the used counter, never lower it."""
    existing = db.scalar(
        select(Rollup).where(
            and_(Rollup.api_key_id == api_key_id, Rollup.month == month)
        )
    )

    if existing:
        # Monotonic upsert: only raise the used counter.
        if used > existing.used:
            existing.used = used
            existing.quota_limit = quota_limit
            db.commit()
    else:
        db.add(Rollup(api_key_id=api_key_id, month=month, used=used, quota_limit=quota_limit))
        db.commit()


def run() -> dict[str, int]:
    """Run the rollup job. Returns stats."""
    pool = get_pool()
    db = SessionLocal()
    lock_token = None
    stats = {
        "scanned": 0,
        "upserted": 0,
        "restored": 0,
        "reconciled": 0,
    }

    try:
        # Acquire lock.
        lock_token = acquire_lock(pool)

        # SCAN Redis quota keys.
        quota_keys = scan_quota_keys(pool)
        stats["scanned"] = len(quota_keys)

        # For each key, find the api_key_id and upsert.
        for redis_key, redis_value in quota_keys:
            parsed = parse_quota_key(redis_key)
            if parsed is None:
                continue

            key_id, month = parsed
            api_key = db.scalar(select(ApiKey).where(ApiKey.key_id == key_id))
            if api_key is None:
                continue

            # Get the effective quota limit (override or plan).
            quota_limit = (
                api_key.override_monthly_quota
                if api_key.override_monthly_quota is not None
                else api_key.plan.monthly_quota
            )

            upsert_rollup(db, api_key.id, month, redis_value, quota_limit)
            stats["upserted"] += 1

    except SQLAlchemyError as exc:
        db.rollback()
        logger.exception("rollup upsert error: %s", exc.__class__.__name__)
    finally:
        if lock_token:
            release_lock(pool, lock_token)
        db.close()

    return stats
