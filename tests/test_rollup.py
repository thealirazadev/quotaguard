"""Rollup job against real Redis and the migrated SQLite database."""

from datetime import UTC, datetime

import pytest

from app import redis_client as app_redis
from app.db import SessionLocal
from app.models import Rollup, WebhookOutbox
from app.services import rollup

# The current month: a past month key is restored with an expiry already in the past.
MONTH = datetime.now(UTC).strftime("%Y-%m")


@pytest.fixture(autouse=True)
async def _clean(redis_client):
    yield
    session = SessionLocal()
    try:
        session.query(WebhookOutbox).delete()
        session.query(Rollup).delete()
        session.commit()
    finally:
        session.close()
    await app_redis.close()


@pytest.fixture
async def key_id(make_plan, make_key):
    await make_plan()
    return (await make_key())["key_id"]


async def test_restore_raises_a_lost_counter_to_its_rollup(redis_client, key_id):
    qkey = f"qg:q:{key_id}:{MONTH}"
    await redis_client.set(qkey, 42)
    assert rollup.run()["upserted"] == 1

    await redis_client.flushdb()
    stats = rollup.run()

    assert stats["restored"] == 1
    assert await redis_client.get(qkey) == "42"
    assert await redis_client.ttl(qkey) > 0


async def test_restore_never_lowers_or_recounts_a_present_counter(redis_client, key_id):
    qkey = f"qg:q:{key_id}:{MONTH}"
    await redis_client.set(qkey, 42)
    rollup.run()

    assert rollup.run()["restored"] == 0
    assert await redis_client.get(qkey) == "42"


async def test_release_lock_never_deletes_a_lock_taken_over_mid_release(redis_client, monkeypatch):
    """A client-side GET then DEL lets another run take the lock in between."""
    pool = app_redis.get_pool()
    token = rollup.acquire_lock(pool)
    original = pool.execute_command
    taken_over = []

    def racing(*args, **kwargs):
        result = original(*args, **kwargs)
        if args[:2] == ("GET", rollup.LOCK_KEY):
            # Our lock expires here and another run acquires it.
            original("SET", rollup.LOCK_KEY, "other-run")
            taken_over.append(True)
        return result

    monkeypatch.setattr(pool, "execute_command", racing)
    rollup.release_lock(pool, token)
    monkeypatch.undo()

    expected = "other-run" if taken_over else None
    assert await redis_client.get(rollup.LOCK_KEY) == expected


async def test_release_lock_leaves_a_lock_held_by_another_run(redis_client):
    pool = app_redis.get_pool()
    token = rollup.acquire_lock(pool)
    with pytest.raises(rollup.LockNotAcquired):
        rollup.acquire_lock(pool)

    await redis_client.set(rollup.LOCK_KEY, "other-run")
    rollup.release_lock(pool, token)
    assert await redis_client.get(rollup.LOCK_KEY) == "other-run"
