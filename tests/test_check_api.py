"""POST /v1/check over HTTP: decision shape, in-band refusals, validation bounds."""

from datetime import UTC, datetime

import pytest

ADMIN_HEADERS = {"X-Admin-Token": "test-admin-token"}

SMALL_PLAN = {
    "slug": "small",
    "burst_capacity": 5,
    "burst_refill_per_sec": 1,
    "sustained_limit": 100,
    "sustained_window_seconds": 60,
    "monthly_quota": 1000,
    "quota_soft_pct": 0,
    "webhook_url": None,
}


@pytest.fixture
async def issued(make_plan, make_key):
    await make_plan(**SMALL_PLAN)
    return await make_key(plan="small")


async def test_an_allowed_check_returns_the_full_decision(script, do_check, issued):
    decision = await do_check(issued["api_key"])

    assert decision["allowed"] is True
    assert decision["reason"] is None
    assert decision["degraded"] is False
    assert decision["retry_after"] is None
    assert decision["headers"]["RateLimit"] == "limit=5, remaining=4, reset=1"
    assert decision["headers"]["RateLimit-Policy"].startswith("5;w=5, 100;w=60, 1000;w=")
    assert "Retry-After" not in decision["headers"]
    assert decision["layers"]["burst"] == {"limit": 5, "remaining": 4, "reset": 1}
    assert decision["layers"]["sustained"] == {"limit": 100, "remaining": 99, "reset": 60}
    quota = decision["layers"]["quota"]
    assert quota["month"] == datetime.now(UTC).strftime("%Y-%m")
    assert (quota["limit"], quota["used"], quota["remaining"]) == (1000, 1, 999)
    assert quota["soft_threshold_crossed"] is False


async def test_a_denied_check_carries_retry_after(script, do_check, issued):
    for _ in range(5):
        assert (await do_check(issued["api_key"]))["allowed"] is True

    decision = await do_check(issued["api_key"])

    assert decision["allowed"] is False
    assert decision["reason"] == "burst"
    assert decision["retry_after"] >= 1
    assert decision["headers"]["Retry-After"] == str(decision["retry_after"])
    assert decision["headers"]["RateLimit"] == "limit=5, remaining=0, reset=5"


async def test_two_resources_limit_independently(script, do_check, issued):
    for _ in range(5):
        assert (await do_check(issued["api_key"], "search"))["allowed"] is True
    assert (await do_check(issued["api_key"], "search"))["allowed"] is False

    other = await do_check(issued["api_key"], "reports")
    assert other["allowed"] is True
    assert other["layers"]["quota"]["used"] == 6


async def test_cost_is_charged_to_the_bucket_and_the_quota(script, do_check, issued):
    decision = await do_check(issued["api_key"], cost=3)

    assert decision["layers"]["burst"]["remaining"] == 2
    assert decision["layers"]["quota"]["used"] == 3
    assert decision["layers"]["sustained"]["remaining"] == 99


async def test_an_unknown_key_is_refused_without_touching_redis(script, do_check, redis_client):
    decision = await do_check("qk_thiskeydoesnotexist")

    assert decision["allowed"] is False
    assert decision["reason"] == "unknown_key"
    assert decision["degraded"] is False
    assert decision["layers"] is None
    assert decision["headers"] == {}
    assert decision["limit"] is None
    assert await redis_client.keys("qg:*") == []


async def test_a_revoked_key_is_refused_immediately(script, client, do_check, issued, redis_client):
    assert (await do_check(issued["api_key"]))["allowed"] is True
    await redis_client.flushdb()

    response = await client.post(f"/admin/keys/{issued['key_id']}/revoke", headers=ADMIN_HEADERS)
    assert response.status_code == 200

    decision = await do_check(issued["api_key"])
    assert decision["allowed"] is False
    assert decision["reason"] == "revoked_key"
    assert decision["layers"] is None
    assert await redis_client.keys("qg:*") == []


async def test_the_quota_layer_reports_the_soft_crossing(script, client, do_check, make_key):
    await client.post(
        "/admin/plans",
        json={
            "slug": "soft",
            "name": "Soft",
            "burst_capacity": 1000,
            "burst_refill_per_sec": 1000,
            "sustained_limit": 1000,
            "sustained_window_seconds": 60,
            "monthly_quota": 10,
            "quota_soft_pct": 80,
            "webhook_url": "https://ops.example.com/hooks/quota",
        },
        headers=ADMIN_HEADERS,
    )
    key = await make_key(plan="soft")

    crossings = []
    for _ in range(9):
        decision = await do_check(key["api_key"])
        crossings.append(decision["layers"]["quota"]["soft_threshold_crossed"])

    assert crossings.count(True) == 1
    assert crossings.index(True) == 7


@pytest.mark.parametrize(
    "body",
    [
        {"resource": "search"},
        {"api_key": "qk_x"},
        {"api_key": "qk_x", "resource": "has space"},
        {"api_key": "qk_x", "resource": ""},
        {"api_key": "qk_x", "resource": "a" * 129},
        {"api_key": "qk_x", "resource": "search", "cost": 0},
        {"api_key": "qk_x", "resource": "search", "cost": 1001},
        {"api_key": "qk_x", "resource": "search", "cost": "many"},
        {"api_key": "qk_x", "resource": "search", "extra": 1},
        {"api_key": "", "resource": "search"},
    ],
)
async def test_invalid_bodies_are_rejected(client, body):
    response = await client.post("/v1/check", json=body)

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"


async def test_malformed_json_is_rejected(client):
    response = await client.post(
        "/v1/check", content="{not json", headers={"Content-Type": "application/json"}
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"


async def test_resource_charset_accepts_the_documented_characters(script, do_check, issued):
    decision = await do_check(issued["api_key"], "v1/search_api.read:all-of-it")
    assert decision["allowed"] is True
