"""Behaviour of app/lua/check.lua against a real Redis: per layer, TTLs, deny purity.

Mocking Redis here would test the mock. Every assertion below inspects the actual
keyspace the script wrote (or did not write).
"""

import asyncio
from datetime import UTC, datetime

from app.services import checker

UTOK = 1_000_000
LOOSE = {"burst_capacity": 1_000_000, "burst_refill_per_sec": 100_000}


def month() -> str:
    return datetime.now(UTC).strftime("%Y-%m")


def keyspace(key_id: str, resource: str = "search") -> tuple[str, str, str, str]:
    bucket, window, quota_prefix, flag_prefix = checker.script_keys(key_id, resource)
    return bucket, window, quota_prefix + month(), flag_prefix + month()


async def snapshot(redis_client, key_id: str, resource: str = "search"):
    bucket, window, quota, flag = keyspace(key_id, resource)
    return (
        await redis_client.hgetall(bucket),
        await redis_client.zrange(window, 0, -1, withscores=True),
        await redis_client.get(quota),
        await redis_client.get(flag),
    )


async def test_burst_denies_after_capacity_and_reports_retry(script, redis_client, resolved):
    key = resolved(key_id="k_burst000001", burst_capacity=5, burst_refill_per_sec=1)
    for index in range(5):
        result = await checker.run_check(key, "search", 1)
        assert result.allowed, index
        assert result.burst_remaining == 4 - index

    denied = await checker.run_check(key, "search", 1)
    assert not denied.allowed
    assert denied.deny_layer == "burst"
    assert denied.burst_remaining == 0
    assert 1 <= denied.retry_after <= 5

    bucket, _, _, _ = keyspace(key.key_id)
    assert 0 <= int(await redis_client.hget(bucket, "t")) < UTOK


async def test_window_trims_and_never_exceeds_the_limit(script, redis_client, resolved):
    key = resolved(key_id="k_window00001", sustained_limit=3, sustained_window_seconds=2, **LOOSE)
    _, window, _, _ = keyspace(key.key_id)

    for _ in range(3):
        assert (await checker.run_check(key, "search", 1)).allowed
    denied = await checker.run_check(key, "search", 1)
    assert not denied.allowed
    assert denied.deny_layer == "sustained"
    assert denied.sustained_remaining == 0
    assert 1 <= denied.retry_after <= 2
    assert await redis_client.zcard(window) == 3

    # A denied check still trims, and still never grows the set.
    assert not (await checker.run_check(key, "search", 1)).allowed
    assert await redis_client.zcard(window) == 3

    await asyncio.sleep(2.1)
    allowed = await checker.run_check(key, "search", 1)
    assert allowed.allowed
    assert await redis_client.zcard(window) == 1


async def test_quota_hard_stop_with_counter_and_ttl(script, redis_client, resolved):
    key = resolved(key_id="k_quota000001", monthly_quota=10, **LOOSE)
    _, _, quota, _ = keyspace(key.key_id)

    for _ in range(10):
        assert (await checker.run_check(key, "search", 1)).allowed
    denied = await checker.run_check(key, "search", 1)
    assert not denied.allowed
    assert denied.deny_layer == "quota"
    assert denied.quota_used == 10
    assert denied.quota_remaining == 0
    assert denied.retry_after == denied.quota_reset > 0

    assert await redis_client.get(quota) == "10"
    ttl = await redis_client.ttl(quota)
    # The month key outlives its month by the 45 day rollup grace.
    assert 3_888_000 < ttl <= 3_888_000 + 31 * 86_400


async def test_soft_threshold_fires_exactly_once(script, redis_client, resolved):
    key = resolved(key_id="k_soft00000001", monthly_quota=10, quota_soft_pct=80, **LOOSE)
    _, _, _, flag = keyspace(key.key_id)

    crossings = []
    for _ in range(10):
        result = await checker.run_check(key, "search", 1)
        crossings.append(result.soft_crossed)

    assert crossings.count(True) == 1
    assert crossings.index(True) == 7, "the crossing check is the one that reaches 8"
    assert await redis_client.get(flag) == "1"
    assert await redis_client.ttl(flag) > 3_888_000


async def test_soft_threshold_is_off_when_pct_is_zero(script, redis_client, resolved):
    key = resolved(key_id="k_nosoft000001", monthly_quota=4, quota_soft_pct=0, **LOOSE)
    _, _, _, flag = keyspace(key.key_id)
    for _ in range(4):
        assert not (await checker.run_check(key, "search", 1)).soft_crossed
    assert await redis_client.get(flag) is None


async def test_a_denied_check_writes_nothing(script, redis_client, resolved):
    key = resolved(key_id="k_pure00000001", monthly_quota=2, **LOOSE)
    for _ in range(2):
        assert (await checker.run_check(key, "search", 1)).allowed

    before = await snapshot(redis_client, key.key_id)
    denied = await checker.run_check(key, "search", 1)
    after = await snapshot(redis_client, key.key_id)

    assert not denied.allowed
    assert before == after


async def test_every_key_carries_a_ttl(script, redis_client, resolved):
    key = resolved(
        key_id="k_ttl000000001",
        burst_capacity=10,
        burst_refill_per_sec=2,
        sustained_window_seconds=30,
        monthly_quota=10,
        quota_soft_pct=10,
    )
    assert (await checker.run_check(key, "search", 1)).allowed
    bucket, window, quota, flag = keyspace(key.key_id)

    # Full refill time plus 60 seconds, and the window plus 60 seconds, minus the
    # milliseconds spent getting here.
    assert 64_000 < await redis_client.pttl(bucket) <= 65_000
    assert 89_000 < await redis_client.pttl(window) <= 90_000
    assert await redis_client.ttl(quota) > 3_888_000
    assert await redis_client.ttl(flag) > 3_888_000


async def test_cost_charges_bucket_and_quota_but_the_window_counts_one(
    script, redis_client, resolved
):
    key = resolved(
        key_id="k_cost00000001",
        burst_capacity=100,
        burst_refill_per_sec=1,
        sustained_limit=10,
        monthly_quota=1000,
    )
    result = await checker.run_check(key, "search", 25)
    _, window, quota, _ = keyspace(key.key_id)

    assert result.allowed
    assert result.burst_remaining == 75
    assert result.quota_used == 25
    assert result.sustained_remaining == 9
    assert await redis_client.zcard(window) == 1
    assert await redis_client.get(quota) == "25"


async def test_a_cost_that_exceeds_the_remaining_budget_denies_whole(script, resolved):
    key = resolved(key_id="k_costdeny0001", burst_capacity=10, burst_refill_per_sec=1)
    assert (await checker.run_check(key, "search", 8)).allowed
    denied = await checker.run_check(key, "search", 5)
    assert not denied.allowed
    assert denied.deny_layer == "burst"
    assert denied.burst_remaining == 2, "nothing was consumed by the denied check"


async def test_resources_limit_independently_but_share_the_quota(script, redis_client, resolved):
    key = resolved(key_id="k_multi0000001", burst_capacity=2, burst_refill_per_sec=1)
    assert (await checker.run_check(key, "search", 1)).allowed
    assert (await checker.run_check(key, "search", 1)).allowed
    assert not (await checker.run_check(key, "search", 1)).allowed

    other = await checker.run_check(key, "reports", 1)
    assert other.allowed
    assert other.burst_remaining == 1
    assert other.quota_used == 3, "the monthly quota is per key, not per resource"

    _, _, quota, _ = keyspace(key.key_id, "reports")
    assert await redis_client.get(quota) == "3"


async def test_the_month_key_is_named_from_the_script_clock(script, redis_client, resolved):
    key = resolved(key_id="k_month0000001", **LOOSE)
    result = await checker.run_check(key, "search", 1)
    assert result.month == month()
    assert await redis_client.exists(f"qg:q:{key.key_id}:{result.month}") == 1


async def test_the_script_survives_a_flushed_script_cache(script, redis_client, resolved):
    """A Redis restart empties the script cache; NOSCRIPT reloads once and retries."""
    key = resolved(key_id="k_noscript0001", **LOOSE)
    assert (await checker.run_check(key, "search", 1)).allowed
    await redis_client.script_flush()
    assert (await checker.run_check(key, "search", 1)).allowed
