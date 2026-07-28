"""No over-admission: N concurrent checks against a budget of M admit exactly min(N, M).

This is the test the whole design exists for, and it is mandatory after any change
to app/lua/check.lua. It fires real concurrent commands at a real Redis; the same
harness run against a check-then-consume implementation admits all 50 (see
docs/memory.md), so a non-atomic regression cannot slip past it.
"""

import asyncio
from datetime import UTC, datetime

from app.services import checker

PARALLEL = 50
BUDGET = 10


async def admit_count(key, resource: str = "search", cost: int = 1) -> list:
    results = await asyncio.gather(
        *(checker.run_check(key, resource, cost) for _ in range(PARALLEL))
    )
    return results


async def test_burst_admits_exactly_the_capacity(script, redis_client, resolved):
    key = resolved(
        key_id="k_conburst001",
        burst_capacity=BUDGET,
        burst_refill_per_sec=1,
        sustained_limit=1_000_000,
        monthly_quota=1_000_000_000,
    )
    results = await admit_count(key)

    assert sum(1 for r in results if r.allowed) == BUDGET
    assert {r.deny_layer for r in results if not r.allowed} == {"burst"}
    bucket = checker.script_keys(key.key_id, "search")[0]
    assert int(await redis_client.hget(bucket, "t")) < 1_000_000


async def test_sustained_admits_exactly_the_limit(script, redis_client, resolved):
    key = resolved(
        key_id="k_consust0001",
        burst_capacity=1_000_000,
        burst_refill_per_sec=100_000,
        sustained_limit=BUDGET,
        sustained_window_seconds=60,
        monthly_quota=1_000_000_000,
    )
    results = await admit_count(key)

    assert sum(1 for r in results if r.allowed) == BUDGET
    assert {r.deny_layer for r in results if not r.allowed} == {"sustained"}
    window = checker.script_keys(key.key_id, "search")[1]
    assert await redis_client.zcard(window) == BUDGET


async def test_quota_admits_exactly_the_remaining_budget(script, redis_client, resolved):
    key = resolved(
        key_id="k_conquota001",
        burst_capacity=1_000_000,
        burst_refill_per_sec=100_000,
        sustained_limit=1_000_000,
        monthly_quota=BUDGET + 5,
    )
    # Spend five sequentially first, so the concurrent burst races a partial budget.
    for _ in range(5):
        assert (await checker.run_check(key, "search", 1)).allowed

    results = await admit_count(key)

    assert sum(1 for r in results if r.allowed) == BUDGET
    assert {r.deny_layer for r in results if not r.allowed} == {"quota"}
    quota = checker.script_keys(key.key_id, "search")[2] + datetime.now(UTC).strftime("%Y-%m")
    assert await redis_client.get(quota) == str(BUDGET + 5)


async def test_soft_threshold_crosses_for_exactly_one_concurrent_caller(
    script, redis_client, resolved
):
    key = resolved(
        key_id="k_consoft0001",
        burst_capacity=1_000_000,
        burst_refill_per_sec=100_000,
        sustained_limit=1_000_000,
        monthly_quota=BUDGET,
        quota_soft_pct=50,
    )
    results = await admit_count(key)

    assert sum(1 for r in results if r.allowed) == BUDGET
    assert sum(1 for r in results if r.soft_crossed) == 1


async def test_cost_is_charged_atomically_under_concurrency(script, redis_client, resolved):
    """Twenty-five parallel checks costing 4 against a quota of 40 admit exactly ten."""
    key = resolved(
        key_id="k_concost0001",
        burst_capacity=1_000_000,
        burst_refill_per_sec=100_000,
        sustained_limit=1_000_000,
        monthly_quota=40,
    )
    results = await asyncio.gather(*(checker.run_check(key, "search", 4) for _ in range(25)))

    assert sum(1 for r in results if r.allowed) == 10
    quota = checker.script_keys(key.key_id, "search")[2] + datetime.now(UTC).strftime("%Y-%m")
    assert await redis_client.get(quota) == "40"


async def test_the_http_endpoint_admits_exactly_the_budget(script, client, make_plan, make_key):
    """The same guarantee through the whole stack: HTTP, key cache, EVALSHA."""
    await make_plan(
        slug="tiny",
        burst_capacity=1_000_000,
        burst_refill_per_sec=100_000,
        sustained_limit=1_000_000,
        sustained_window_seconds=60,
        monthly_quota=BUDGET,
        quota_soft_pct=0,
        webhook_url=None,
    )
    key = await make_key(plan="tiny")
    payload = {"api_key": key["api_key"], "resource": "search"}

    responses = await asyncio.gather(
        *(client.post("/v1/check", json=payload) for _ in range(PARALLEL))
    )
    decisions = [response.json()["data"] for response in responses]

    assert all(response.status_code == 200 for response in responses)
    assert sum(1 for d in decisions if d["allowed"]) == BUDGET
    assert {d["reason"] for d in decisions if not d["allowed"]} == {"quota"}
