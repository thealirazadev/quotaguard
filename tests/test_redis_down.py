"""Per-plan Redis-down policy on the check path, and health reporting."""

import asyncio
import logging

import pytest
from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import NoScriptError
from redis.exceptions import TimeoutError as RedisTimeoutError

from app import redis_client
from app.redis_client import build_client
from app.services import checker
from tests.conftest import TEST_REDIS_URL

# Charges first, then outlives a 100 ms socket timeout: the shape of a check script that
# consumed but whose reply arrived too late.
SLOW_CHARGE = """
local start = redis.call('TIME')
local start_us = tonumber(start[1]) * 1000000 + tonumber(start[2])
redis.call('INCR', KEYS[1])
while true do
  local now = redis.call('TIME')
  if tonumber(now[1]) * 1000000 + tonumber(now[2]) - start_us > 300000 then
    break
  end
end
return 1
"""

PLAN = {
    "burst_capacity": 5,
    "burst_refill_per_sec": 1,
    "sustained_limit": 100,
    "sustained_window_seconds": 60,
    "monthly_quota": 1000,
    "quota_soft_pct": 0,
    "webhook_url": None,
}


@pytest.fixture
def redis_fails(monkeypatch):
    def _fail(error: Exception) -> None:
        async def broken(*args, **kwargs):
            raise error

        monkeypatch.setattr(checker, "run_check", broken)

    return _fail


@pytest.mark.parametrize(
    "error",
    [RedisConnectionError("refused"), RedisTimeoutError("slow"), NoScriptError("gone")],
    ids=["connect", "timeout", "noscript"],
)
async def test_fail_open_allows_with_a_degraded_decision(
    script, client, make_plan, make_key, do_check, redis_fails, error, caplog, monkeypatch
):
    monkeypatch.setattr(logging.getLogger("quotaguard"), "propagate", True)
    monkeypatch.setattr(logging.getLogger("quotaguard.check"), "disabled", False)
    await make_plan(slug="open", redis_down_policy="fail_open", **PLAN)
    key = await make_key(plan="open")
    redis_fails(error)

    with caplog.at_level(logging.WARNING, logger="quotaguard.check"):
        decision = await do_check(key["api_key"])

    assert decision == {
        "allowed": True,
        "reason": None,
        "degraded": True,
        "limit": None,
        "remaining": None,
        "reset": None,
        "retry_after": None,
        "headers": {},
        "layers": None,
    }
    events = [getattr(r, "event", None) for r in caplog.records]
    assert events.count("check.redis_down") == 1


@pytest.mark.parametrize(
    "error",
    [RedisConnectionError("refused"), RedisTimeoutError("slow")],
    ids=["connect", "timeout"],
)
async def test_fail_closed_refuses_with_redis_unavailable(
    script, client, make_plan, make_key, do_check, redis_fails, error
):
    await make_plan(slug="closed", redis_down_policy="fail_closed", **PLAN)
    key = await make_key(plan="closed")
    redis_fails(error)

    decision = await do_check(key["api_key"])

    assert (decision["allowed"], decision["reason"], decision["degraded"]) == (
        False,
        "redis_unavailable",
        True,
    )


async def test_checks_recover_when_redis_returns(script, client, make_plan, make_key, do_check):
    await make_plan(slug="open", redis_down_policy="fail_open", **PLAN)
    key = await make_key(plan="open")
    await redis_client.get_client().script_flush()  # what a Redis restart does to the cache

    decision = await do_check(key["api_key"])

    assert decision["degraded"] is False and decision["allowed"] is True
    assert decision["layers"]["burst"]["remaining"] == 4


async def test_health_reports_redis_down_without_failing(client, monkeypatch):
    class Down:
        async def ping(self):
            raise RedisConnectionError("refused")

    monkeypatch.setattr(redis_client, "get_client", lambda: Down())
    response = await client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "redis": "down"}


async def test_a_timeout_after_the_script_ran_is_never_retried(redis_client):
    """A retried consume would charge the same check twice; at most one charge is allowed."""
    client = build_client(TEST_REDIS_URL, timeout_seconds=0.1)
    try:
        with pytest.raises(RedisTimeoutError):
            await client.eval(SLOW_CHARGE, 1, "qg:test:charges")
    finally:
        await client.aclose()

    await asyncio.sleep(0.5)  # long enough for a retried run, had there been one, to finish
    assert await redis_client.get("qg:test:charges") == "1"
