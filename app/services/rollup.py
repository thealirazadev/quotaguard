"""Monthly quota rollup: scan Redis counters, upsert to SQLite idempotently, restore after loss."""

import calendar
import logging
import uuid
from datetime import UTC, datetime

from redis.exceptions import RedisError
from sqlalchemy import and_, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.db import SessionLocal
from app.models import ApiKey, Rollup, utcnow
from app.redis_client import get_pool, get_registry

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


def month_end_seconds(year: int, month: int) -> int:
    """Epoch seconds of the first instant of the next month (month-end in UTC)."""
    if month == 12:
        next_month_date = datetime(year + 1, 1, 1, tzinfo=UTC)
    else:
        next_month_date = datetime(year, month + 1, 1, tzinfo=UTC)
    return int(next_month_date.timestamp())


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


def restore_from_rollup(pool, key_id: str, month: str, rollup_value: int) -> int:
    """Restore a quota counter from its rollup value if it was lost.

    Uses restore.lua for atomic restore: if the current value is below the rollup,
    raise it to the rollup (delta-based INCRBY). Returns count of restored keys (0 or 1).
    """
    try:
        year, month_num = int(month[:4]), int(month[5:7])
        month_end = month_end_seconds(year, month_num)

        redis_key = f"qg:q:{key_id}:{month}"
        registry = get_registry()

        if "restore" not in registry.scripts:
            logger.warning("restore.lua not loaded in script registry")
            return 0

        result = registry.scripts["restore"](keys=[redis_key], args=[rollup_value, month_end])
        if result != rollup_value:
            # The current value was not below the rollup; no restore needed.
            return 0
        return 1
    except Exception as exc:
        logger.warning("restore failed for %s %s: %s", key_id, month, exc.__class__.__name__)
        return 0


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

        # Phase 2: Restore from rollups (for counters that were lost).
        rollups = db.query(Rollup).all()
        for rollup in rollups:
            api_key = db.scalar(select(ApiKey).where(ApiKey.id == rollup.api_key_id))
            if api_key is None:
                continue
            restored = restore_from_rollup(pool, api_key.key_id, rollup.month, rollup.used)
            stats["restored"] += restored

    except SQLAlchemyError as exc:
        db.rollback()
        logger.exception("rollup upsert error: %s", exc.__class__.__name__)
    finally:
        if lock_token:
            release_lock(pool, lock_token)
        db.close()

    return stats
