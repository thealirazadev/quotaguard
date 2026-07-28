"""Mirror-math for the token bucket: the Lua refill re-implemented and cross-checked.

The mirror is asserted against exact rational arithmetic (never over-credit, never
exceed capacity, never advance the stored timestamp past now) and then against the
real script running on Redis.
"""

import asyncio
import math
import random
from fractions import Fraction

from app.services import checker

UTOK = 1_000_000
# Slack for the round trip between reading TIME here and the script reading its own.
TOLERANCE_US = 250_000


def refill(tokens: int, ts: int, now_us: int, cap_utok: int, rate_utok: int) -> tuple[int, int]:
    """The refill of app/lua/check.lua step 3, line for line."""
    elapsed = max(0, now_us - ts)
    if elapsed >= (cap_utok / rate_utok) * UTOK:
        return cap_utok, now_us
    add = math.floor((elapsed / UTOK) * rate_utok)
    if add > 0:
        tokens = tokens + add
        if tokens >= cap_utok:
            return cap_utok, now_us
        ts = ts + math.ceil(add * UTOK / rate_utok)
    return tokens, ts


def remaining_after(tokens: int, cost_utok: int) -> int:
    """What the script reports post-decision: the cost is charged only if it fits."""
    if tokens >= cost_utok:
        return (tokens - cost_utok) // UTOK
    return tokens // UTOK


def test_mirror_never_over_credits_and_stays_in_bounds():
    rng = random.Random(20260729)
    now_us = 1_785_000_000_000_000
    for _ in range(5000):
        cap_utok = rng.randrange(1, 1_000_001) * UTOK
        rate_utok = rng.randrange(1, 100_001) * UTOK
        start = rng.randrange(0, cap_utok + 1)
        elapsed = rng.randrange(0, int(cap_utok / rate_utok * UTOK) + 2)
        tokens, ts = refill(start, now_us - elapsed, now_us, cap_utok, rate_utok)
        exact = start + Fraction(elapsed * rate_utok, UTOK)
        assert tokens <= min(cap_utok, exact), "refill credited more than the elapsed time"
        assert 0 <= tokens <= cap_utok
        assert ts <= now_us, "the stored timestamp may never move into the future"


def test_mirror_carries_the_fractional_remainder():
    """A sub-token elapsed credits nothing but is not thrown away either."""
    cap_utok, rate_utok = 5 * UTOK, 1 * UTOK
    now_us = 1_785_000_000_000_000
    tokens, ts = refill(0, now_us - 400_000, now_us, cap_utok, rate_utok)
    assert tokens == 400_000
    assert ts == now_us - 400_000 + 400_000


def test_mirror_clamps_a_regressed_clock():
    cap_utok, rate_utok = 5 * UTOK, 1 * UTOK
    now_us = 1_785_000_000_000_000
    tokens, ts = refill(2 * UTOK, now_us + 5_000_000, now_us, cap_utok, rate_utok)
    assert tokens == 2 * UTOK
    assert ts == now_us + 5_000_000


async def _now_us(redis_client) -> int:
    seconds, micros = await redis_client.time()
    return seconds * UTOK + micros


async def test_script_refill_matches_the_mirror(script, redis_client, resolved):
    """Seed a bucket in the past, then check that the script credits what the mirror does."""
    rng = random.Random(4242)
    for index in range(20):
        capacity = rng.choice([1, 5, 100, 5000, 1_000_000])
        rate = rng.choice([1, 7, 500, 100_000])
        key = resolved(
            key_id=f"k_refill{index:06d}", burst_capacity=capacity, burst_refill_per_sec=rate
        )
        cap_utok, rate_utok = capacity * UTOK, rate * UTOK
        bucket = checker.script_keys(key.key_id, "search")[0]
        start = rng.randrange(0, cap_utok + 1)
        elapsed = rng.randrange(0, int(cap_utok / rate_utok * UTOK) + 2)
        now_us = await _now_us(redis_client)
        await redis_client.hset(bucket, mapping={"t": start, "ts": now_us - elapsed})

        result = await checker.run_check(key, "search", 1)

        low, _ = refill(start, now_us - elapsed, now_us, cap_utok, rate_utok)
        high, _ = refill(start, now_us - elapsed, now_us + TOLERANCE_US, cap_utok, rate_utok)
        assert (
            remaining_after(low, UTOK) <= result.burst_remaining <= remaining_after(high, UTOK)
        ), f"capacity={capacity} rate={rate} start={start} elapsed={elapsed}"
        assert result.burst_limit == capacity


async def test_script_refills_over_real_time(script, redis_client, resolved):
    """Capacity 5 at 1 token per second: drained, then two tokens back after ~2 seconds."""
    key = resolved(key_id="k_realtime001", burst_capacity=5, burst_refill_per_sec=1)
    for _ in range(5):
        assert (await checker.run_check(key, "search", 1)).allowed
    denied = await checker.run_check(key, "search", 1)
    assert not denied.allowed and denied.deny_layer == "burst"
    assert denied.retry_after >= 1

    await asyncio.sleep(2.1)
    assert (await checker.run_check(key, "search", 1)).allowed
    assert (await checker.run_check(key, "search", 1)).allowed
    third = await checker.run_check(key, "search", 1)
    assert not third.allowed and third.deny_layer == "burst"
    assert third.burst_remaining == 0


async def test_script_never_reports_negative_remaining(script, resolved):
    key = resolved(key_id="k_neg00000001", burst_capacity=2, burst_refill_per_sec=1)
    for _ in range(6):
        result = await checker.run_check(key, "search", 1)
        assert result.burst_remaining >= 0
        assert result.sustained_remaining >= 0
        assert result.quota_remaining >= 0
