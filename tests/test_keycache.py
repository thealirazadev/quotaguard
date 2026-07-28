"""Key cache: resolution, effective limits, TTL, and invalidation on admin writes."""

import pytest

from app.services import keycache


@pytest.fixture(autouse=True)
def _empty_cache():
    keycache.invalidate()
    yield
    keycache.invalidate()


@pytest.fixture
def counted_loads(monkeypatch):
    """Count real SQLite lookups so cache hits are observable."""
    calls = []
    original = keycache._load

    def _counting(digest):
        calls.append(digest)
        return original(digest)

    monkeypatch.setattr(keycache, "_load", _counting)
    return calls


async def test_unknown_key_resolves_to_none(client):
    assert await keycache.resolve("qk_nothing") is None


async def test_resolve_returns_plan_limits_and_policy(client, make_plan, make_key):
    await make_plan(redis_down_policy="fail_closed")
    issued = await make_key()

    key = await keycache.resolve(issued["api_key"])

    assert key is not None
    assert key.key_id == issued["key_id"]
    assert key.plan_slug == "pro"
    assert key.revoked is False
    assert key.burst_capacity == 100
    assert key.burst_refill_per_sec == 50
    assert key.sustained_limit == 5000
    assert key.sustained_window_seconds == 3600
    assert key.monthly_quota == 500000
    assert key.quota_soft_pct == 80
    assert key.redis_down_policy == "fail_closed"


async def test_overrides_win_over_plan_values(client, make_plan, make_key):
    await make_plan()
    issued = await make_key()
    await client.patch(
        f"/admin/keys/{issued['key_id']}",
        json={"override_sustained_limit": 10000},
        headers={"X-Admin-Token": "test-admin-token"},
    )

    key = await keycache.resolve(issued["api_key"])

    assert key.sustained_limit == 10000
    assert key.monthly_quota == 500000


async def test_a_second_resolve_is_served_from_memory(client, make_plan, make_key, counted_loads):
    await make_plan()
    issued = await make_key()

    first = await keycache.resolve(issued["api_key"])
    second = await keycache.resolve(issued["api_key"])

    assert first == second
    assert len(counted_loads) == 1


async def test_unknown_keys_are_cached_too(client, counted_loads):
    assert await keycache.resolve("qk_bogus") is None
    assert await keycache.resolve("qk_bogus") is None
    assert len(counted_loads) == 1


async def test_revoking_a_key_invalidates_the_cache(client, make_plan, make_key, counted_loads):
    await make_plan()
    issued = await make_key()
    assert (await keycache.resolve(issued["api_key"])).revoked is False

    await client.post(
        f"/admin/keys/{issued['key_id']}/revoke", headers={"X-Admin-Token": "test-admin-token"}
    )

    key = await keycache.resolve(issued["api_key"])
    assert key.revoked is True
    assert len(counted_loads) == 2, "the revoke dropped the cached entry"


async def test_updating_a_plan_invalidates_the_cache(client, make_plan, make_key):
    await make_plan()
    issued = await make_key()
    assert (await keycache.resolve(issued["api_key"])).monthly_quota == 500000

    await client.patch(
        "/admin/plans/pro",
        json={"monthly_quota": 42},
        headers={"X-Admin-Token": "test-admin-token"},
    )

    assert (await keycache.resolve(issued["api_key"])).monthly_quota == 42
