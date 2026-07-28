"""TTL cache mapping sha256(api_key) to the resolved key, limits, and plan policy.

The check path must not touch SQLite per request, so a hit answers from process
memory and a miss resolves in a threadpool (the check route is async). Admin
mutations invalidate the whole cache in-process; anything else is bounded by
KEY_CACHE_TTL_SECONDS.
"""

import logging
from dataclasses import dataclass
from time import monotonic

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import joinedload
from starlette.concurrency import run_in_threadpool

from app.config import get_settings
from app.db import SessionLocal
from app.errors import InternalError
from app.models import ApiKey
from app.services import keys as keys_service

logger = logging.getLogger("quotaguard.keycache")


@dataclass(frozen=True)
class ResolvedKey:
    """Everything the check path needs about a key, with overrides already applied."""

    key_id: str
    name: str
    plan_slug: str
    revoked: bool
    burst_capacity: int
    burst_refill_per_sec: int
    sustained_limit: int
    sustained_window_seconds: int
    monthly_quota: int
    quota_soft_pct: int
    redis_down_policy: str


# digest -> (expires_at monotonic, resolved key or None for a known-unknown key)
_cache: dict[str, tuple[float, ResolvedKey | None]] = {}


def invalidate() -> None:
    """Called by every plan and key mutation; the cache is small enough to drop whole."""
    _cache.clear()


def _load(digest: str) -> ResolvedKey | None:
    session = SessionLocal()
    try:
        key = session.scalar(
            select(ApiKey).options(joinedload(ApiKey.plan)).where(ApiKey.key_hash == digest)
        )
        if key is None:
            return None
        limits = key.effective_limits()
        return ResolvedKey(
            key_id=key.key_id,
            name=key.name,
            plan_slug=key.plan.slug,
            revoked=key.revoked_at is not None,
            quota_soft_pct=key.plan.quota_soft_pct,
            redis_down_policy=key.plan.redis_down_policy,
            **limits,
        )
    except SQLAlchemyError as exc:
        logger.exception("key lookup failed: %s", exc.__class__.__name__)
        raise InternalError("The key could not be looked up.") from exc
    finally:
        session.close()


async def resolve(api_key: str) -> ResolvedKey | None:
    """Resolve a plaintext secret. None means no key with that hash exists."""
    # Imported as a module, not a symbol: keys.py invalidates this cache, and the
    # attribute lookup happens at call time so the two modules can import each other.
    digest = keys_service.hash_secret(api_key)
    entry = _cache.get(digest)
    now = monotonic()
    if entry is not None and entry[0] > now:
        return entry[1]
    resolved = await run_in_threadpool(_load, digest)
    ttl = get_settings().key_cache_ttl_seconds
    if ttl > 0:
        # Misses are cached too, so a flood of bogus keys cannot turn into a
        # SQLite query per request; a real secret is random enough never to
        # collide with a cached miss.
        _cache[digest] = (now + ttl, resolved)
    return resolved
