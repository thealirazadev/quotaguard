"""Latency smoke test: one Redis round trip per check and a small local p50."""

import statistics
import time

from app import redis_client

PLAN = {
    "slug": "fast",
    "burst_capacity": 1000,
    "burst_refill_per_sec": 1000,
    "sustained_limit": 10000,
    "sustained_window_seconds": 60,
    "monthly_quota": 100000,
    "quota_soft_pct": 0,
    "webhook_url": None,
}
CHECKS = 50
# Commands the application could send directly (anything else ran inside check.lua).
CLIENT_COMMANDS = {
    "cmdstat_evalsha",
    "cmdstat_eval",
    "cmdstat_script|load",
    "cmdstat_info",
    "cmdstat_ping",
    "cmdstat_multi",
    "cmdstat_exec",
}


async def commands_processed() -> dict[str, int]:
    stats = await redis_client.get_client().info("commandstats")
    return {name: int(value["calls"]) for name, value in stats.items()}


async def test_each_check_is_one_redis_command_and_fast(script, make_plan, make_key, do_check):
    await make_plan(**PLAN)
    key = await make_key(plan="fast")
    await do_check(key["api_key"])  # warm the key cache and the script

    before = await commands_processed()
    timings = []
    for _ in range(CHECKS):
        started = time.perf_counter()
        await do_check(key["api_key"])
        timings.append(time.perf_counter() - started)
    after = await commands_processed()

    delta = {k: after.get(k, 0) - before.get(k, 0) for k in after}
    # Commands run inside the Lua script are counted too; the client sent only EVALSHA.
    client_sent = {k for k in delta if delta[k] and k in CLIENT_COMMANDS}
    assert delta["cmdstat_evalsha"] == CHECKS
    assert client_sent == {"cmdstat_evalsha", "cmdstat_info"}
    assert statistics.median(timings) < 0.05
