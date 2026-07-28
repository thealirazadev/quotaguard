"""Plan admin surface: creation, bounds, conflicts, listing, and updates."""

import pytest

from tests.conftest import ADMIN_HEADERS, PLAN_PAYLOAD


async def test_create_plan_returns_the_stored_plan(client):
    response = await client.post("/admin/plans", json=PLAN_PAYLOAD, headers=ADMIN_HEADERS)

    assert response.status_code == 201
    plan = response.json()["data"]
    assert plan["slug"] == "pro"
    assert plan["redis_down_policy"] == "fail_open"
    assert plan["created_at"].endswith("Z")


async def test_duplicate_slug_conflicts(client, make_plan):
    await make_plan()

    response = await client.post("/admin/plans", json=PLAN_PAYLOAD, headers=ADMIN_HEADERS)

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "conflict"


@pytest.mark.parametrize(
    "field,value",
    [
        ("burst_capacity", 0),
        ("burst_capacity", 1_000_001),
        ("burst_refill_per_sec", 100_001),
        ("sustained_limit", 0),
        ("sustained_window_seconds", 86_401),
        ("monthly_quota", 1_000_000_000_001),
        ("quota_soft_pct", 101),
        ("slug", "Not A Slug"),
        ("name", ""),
        ("redis_down_policy", "fail_sometimes"),
        ("webhook_url", "ftp://ops.example.com"),
    ],
)
async def test_out_of_bounds_values_are_rejected(client, field, value):
    response = await client.post(
        "/admin/plans", json={**PLAN_PAYLOAD, field: value}, headers=ADMIN_HEADERS
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"


async def test_unknown_fields_are_rejected(client):
    response = await client.post(
        "/admin/plans", json={**PLAN_PAYLOAD, "surprise": 1}, headers=ADMIN_HEADERS
    )

    assert response.status_code == 422


async def test_soft_threshold_requires_a_webhook_url(client):
    payload = {**PLAN_PAYLOAD, "webhook_url": None}

    response = await client.post("/admin/plans", json=payload, headers=ADMIN_HEADERS)

    assert response.status_code == 422
    assert "webhook_url" in response.json()["error"]["message"]


async def test_list_is_ordered_by_slug(client, make_plan):
    await make_plan()
    await make_plan(slug="free", name="Free", quota_soft_pct=0, webhook_url=None)

    response = await client.get("/admin/plans", headers=ADMIN_HEADERS)

    body = response.json()["data"]
    assert body["total"] == 2
    assert [plan["slug"] for plan in body["plans"]] == ["free", "pro"]


async def test_empty_list_is_well_formed(client):
    response = await client.get("/admin/plans", headers=ADMIN_HEADERS)

    assert response.status_code == 200
    assert response.json()["data"] == {"plans": [], "total": 0}


async def test_detail_reports_key_count(client, make_plan, make_key):
    await make_plan()
    await make_key()

    response = await client.get("/admin/plans/pro", headers=ADMIN_HEADERS)

    assert response.json()["data"]["keys_count"] == 1


async def test_unknown_plan_is_not_found(client):
    response = await client.get("/admin/plans/nope", headers=ADMIN_HEADERS)

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


async def test_patch_updates_only_the_given_fields(client, make_plan):
    await make_plan()

    response = await client.patch(
        "/admin/plans/pro", json={"sustained_limit": 10_000}, headers=ADMIN_HEADERS
    )

    plan = response.json()["data"]
    assert plan["sustained_limit"] == 10_000
    assert plan["burst_capacity"] == PLAN_PAYLOAD["burst_capacity"]


async def test_patch_rejects_out_of_bounds_and_null_values(client, make_plan):
    await make_plan()

    bounds = await client.patch(
        "/admin/plans/pro", json={"sustained_limit": 0}, headers=ADMIN_HEADERS
    )
    nulled = await client.patch("/admin/plans/pro", json={"name": None}, headers=ADMIN_HEADERS)

    assert bounds.status_code == 422
    assert nulled.status_code == 422
