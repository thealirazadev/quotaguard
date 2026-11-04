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
