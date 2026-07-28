"""Key admin surface: issuing, secret handling, overrides, and revocation."""

from sqlalchemy import select

from app.models import ApiKey
from tests.conftest import ADMIN_HEADERS


async def test_issue_returns_the_secret_once_and_stores_only_its_hash(
    client, make_plan, make_key, db
):
    await make_plan()
    issued = await make_key()

    secret = issued["api_key"]
    assert secret.startswith("qk_")
    assert len(secret) == 46
    assert issued["key_id"].startswith("k_")
    assert len(issued["key_id"]) == 14
    assert issued["key_prefix"] == secret[:12]

    stored = db.scalar(select(ApiKey).where(ApiKey.key_id == issued["key_id"]))
    assert stored.key_hash != secret
    assert len(stored.key_hash) == 64
    assert secret not in (stored.key_hash, stored.key_prefix)


async def test_listing_never_exposes_the_secret(client, make_plan, make_key):
    await make_plan()
    issued = await make_key()

    response = await client.get("/admin/keys", headers=ADMIN_HEADERS)

    body = response.text
    assert issued["api_key"] not in body
    assert issued["key_prefix"] in body


async def test_issue_for_unknown_plan_is_not_found(client):
    response = await client.post(
        "/admin/keys", json={"plan": "nope", "name": "x"}, headers=ADMIN_HEADERS
    )

    assert response.status_code == 404


async def test_list_filters_by_plan(client, make_plan, make_key):
    await make_plan()
    await make_plan(slug="free", name="Free", quota_soft_pct=0, webhook_url=None)
    await make_key()
    await make_key(plan="free", name="free tier")

    everything = await client.get("/admin/keys", headers=ADMIN_HEADERS)
    filtered = await client.get("/admin/keys?plan=free", headers=ADMIN_HEADERS)

    assert everything.json()["data"]["total"] == 2
    assert filtered.json()["data"]["total"] == 1
    assert filtered.json()["data"]["keys"][0]["plan"] == "free"


async def test_empty_key_list_is_well_formed(client):
    response = await client.get("/admin/keys", headers=ADMIN_HEADERS)

    assert response.json()["data"] == {"keys": [], "total": 0}


async def test_detail_resolves_effective_limits(client, make_plan, make_key):
    await make_plan()
    issued = await make_key()

    response = await client.get(f"/admin/keys/{issued['key_id']}", headers=ADMIN_HEADERS)

    detail = response.json()["data"]
    assert detail["overrides"] == {}
    assert detail["effective_limits"]["sustained_limit"] == 5000


async def test_unknown_key_is_not_found(client):
    response = await client.get("/admin/keys/k_missing", headers=ADMIN_HEADERS)

    assert response.status_code == 404


async def test_override_can_be_set_and_cleared(client, make_plan, make_key):
    await make_plan()
    key_id = (await make_key())["key_id"]

    set_response = await client.patch(
        f"/admin/keys/{key_id}", json={"override_sustained_limit": 10_000}, headers=ADMIN_HEADERS
    )
    cleared_response = await client.patch(
        f"/admin/keys/{key_id}", json={"override_sustained_limit": None}, headers=ADMIN_HEADERS
    )

    assert set_response.json()["data"]["overrides"] == {"override_sustained_limit": 10_000}
    assert set_response.json()["data"]["effective_limits"]["sustained_limit"] == 10_000
    assert cleared_response.json()["data"]["overrides"] == {}
    assert cleared_response.json()["data"]["effective_limits"]["sustained_limit"] == 5000


async def test_override_respects_the_plan_bounds(client, make_plan, make_key):
    await make_plan()
    key_id = (await make_key())["key_id"]

    response = await client.patch(
        f"/admin/keys/{key_id}", json={"override_sustained_limit": 0}, headers=ADMIN_HEADERS
    )

    assert response.status_code == 422


async def test_empty_patch_is_rejected(client, make_plan, make_key):
    await make_plan()
    key_id = (await make_key())["key_id"]

    response = await client.patch(f"/admin/keys/{key_id}", json={}, headers=ADMIN_HEADERS)

    assert response.status_code == 422


async def test_revoke_is_permanent_and_not_repeatable(client, make_plan, make_key):
    await make_plan()
    key_id = (await make_key())["key_id"]

    first = await client.post(f"/admin/keys/{key_id}/revoke", headers=ADMIN_HEADERS)
    second = await client.post(f"/admin/keys/{key_id}/revoke", headers=ADMIN_HEADERS)
    patched = await client.patch(
        f"/admin/keys/{key_id}", json={"name": "renamed"}, headers=ADMIN_HEADERS
    )

    assert first.status_code == 200
    assert first.json()["data"]["revoked_at"].endswith("Z")
    assert second.status_code == 409
    assert patched.status_code == 409


async def test_admin_writes_touch_no_redis_keys(client, make_plan, make_key, redis_client):
    """Phase 1 is a configuration surface: nothing here may write admission state."""
    await make_plan()
    key_id = (await make_key())["key_id"]
    await client.patch(
        f"/admin/keys/{key_id}", json={"override_monthly_quota": 99}, headers=ADMIN_HEADERS
    )
    await client.post(f"/admin/keys/{key_id}/revoke", headers=ADMIN_HEADERS)

    assert await redis_client.dbsize() == 0
