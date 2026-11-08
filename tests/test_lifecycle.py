"""Phase 3: rollups, restore after Redis loss, webhook reconciliation, and usage reports."""

import json

import pytest
from sqlalchemy import select

from app.models import Rollup, WebhookOutbox
from app.redis_client import get_pool
from app.schemas.plans import PlanCreateRequest
from app.services import keys as keys_service
from app.services import plans as plans_service
from app.services import rollup
from app.services.usage import get_current_month

ADMIN = {"X-Admin-Token": "test-admin-token"}


@pytest.fixture
def pool():
    connection = get_pool()
    connection.flushdb()
    yield connection
    connection.flushdb()


@pytest.fixture
def key(db):
    plans_service.create_plan(
        db,
        PlanCreateRequest(
            slug="trial",
            name="Trial",
            burst_capacity=100,
            burst_refill_per_sec=50,
            sustained_limit=1000,
            sustained_window_seconds=60,
            monthly_quota=10,
            quota_soft_pct=80,
            webhook_url="http://127.0.0.1:9/hook",
        ),
    )
    api_key, _secret = keys_service.issue_key(db, "trial", "acme")
    return api_key


def counter(key_id: str, month: str | None = None) -> str:
    return f"qg:q:{key_id}:{month or get_current_month()}"


def rollups(db) -> list[tuple[str, int]]:
    db.expire_all()
    return [(r.month, r.used) for r in db.scalars(select(Rollup).order_by(Rollup.month))]


def test_rollup_is_idempotent_and_never_lowers_a_persisted_value(db, pool, key):
    pool.set(counter(key.key_id), 7)
    rollup.run()
    rollup.run()
    assert rollups(db) == [(get_current_month(), 7)]

    pool.set(counter(key.key_id), 3)  # a stale or partially restored counter
    rollup.run()
    assert rollups(db) == [(get_current_month(), 7)]


def test_restore_raises_a_lost_counter_back_to_the_rollup(db, pool, key):
    pool.set(counter(key.key_id), 9)
    rollup.run()
    pool.flushdb()
    pool.incrby(counter(key.key_id), 1)  # live traffic after the loss

    stats = rollup.run()

    assert stats["restored"] == 1
    assert int(pool.get(counter(key.key_id))) == 9
    assert pool.ttl(counter(key.key_id)) > 0


def test_a_counter_at_or_above_its_rollup_is_left_alone(db, pool, key):
    pool.set(counter(key.key_id), 5)
    rollup.run()
    pool.incrby(counter(key.key_id), 2)
    assert rollup.run()["restored"] == 0
    assert int(pool.get(counter(key.key_id))) == 7


def test_a_held_lock_skips_the_run_and_is_not_released_by_it(db, pool, key):
    pool.set(rollup.LOCK_KEY, "someone-else", px=60_000)
    with pytest.raises(rollup.LockNotAcquired):
        rollup.run()
    assert pool.get(rollup.LOCK_KEY) == "someone-else"
    pool.delete(rollup.LOCK_KEY)

    token = rollup.acquire_lock(pool)
    rollup.release_lock(pool, "not-the-token")
    assert pool.get(rollup.LOCK_KEY) == token
    rollup.release_lock(pool, token)
    assert pool.get(rollup.LOCK_KEY) is None


def test_reconciliation_enqueues_a_missed_warning_exactly_once(db, pool, key):
    month = get_current_month()
    pool.set(counter(key.key_id), 9)
    pool.set(f"qg:f:{key.key_id}:{month}", 1)

    assert rollup.run()["reconciled"] == 1
    assert rollup.run()["reconciled"] == 0

    rows = db.scalars(select(WebhookOutbox)).all()
    assert len(rows) == 1
    payload = json.loads(rows[0].payload)
    assert (payload["key_id"], payload["used"], payload["quota"]) == (key.key_id, 9, 10)


async def test_usage_reports_live_month_then_rollup_history_without_duplicates(
    client, db, pool, key
):
    month = get_current_month()
    db.add(Rollup(api_key_id=key.id, month="2026-01", used=4, quota_limit=10))
    db.add(Rollup(api_key_id=key.id, month="2025-12", used=10, quota_limit=10))
    db.add(WebhookOutbox(api_key_id=key.id, month="2025-12", kind="quota_soft", payload="{}"))
    db.commit()
    pool.set(counter(key.key_id), 6)
    rollup.run()  # writes a current-month rollup row too

    response = await client.get(f"/v1/usage/{key.key_id}", params={"months": 3}, headers=ADMIN)

    months = response.json()["data"]["months"]
    assert [(m["month"], m["used"], m["source"]) for m in months] == [
        (month, 6, "live"),
        ("2026-01", 4, "rollup"),
        ("2025-12", 10, "rollup"),
    ]
    assert months[0]["remaining"] == 4
    assert [m["soft_threshold_crossed"] for m in months] == [False, False, True]


async def test_usage_falls_back_to_the_rollup_when_redis_lost_the_month(client, db, pool, key):
    pool.set(counter(key.key_id), 8)
    rollup.run()
    pool.flushdb()

    response = await client.get(f"/v1/usage/{key.key_id}", headers=ADMIN)

    assert response.json()["data"]["months"][0] == {
        "month": get_current_month(),
        "used": 8,
        "quota": 10,
        "remaining": 2,
        "soft_threshold_crossed": False,
        "source": "rollup",
    }


async def test_usage_rejects_unknown_keys_bad_months_and_missing_tokens(client, key):
    assert (await client.get("/v1/usage/k_missing", headers=ADMIN)).status_code == 404
    bad = await client.get(f"/v1/usage/{key.key_id}", params={"months": 25}, headers=ADMIN)
    assert bad.status_code == 422
    assert (await client.get(f"/v1/usage/{key.key_id}")).status_code == 401
