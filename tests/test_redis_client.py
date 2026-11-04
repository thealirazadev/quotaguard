"""The synchronous pool used by the background jobs."""

from app import redis_client as app_redis


async def test_sync_pool_decodes_replies_and_is_rebuilt_after_close(redis_client):
    pool = app_redis.get_pool()
    pool.set("qg:test:pool", "value")
    assert pool.get("qg:test:pool") == "value"

    await app_redis.close()
    assert app_redis.get_pool() is not pool
    await app_redis.close()
