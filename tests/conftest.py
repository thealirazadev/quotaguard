"""Shared fixtures.

The environment is fixed before any application module is imported, so settings,
the engine, and the test database all agree. A real Redis is required.
"""

import os
import tempfile
from pathlib import Path

ADMIN_TOKEN = "test-admin-token"
TEST_REDIS_URL = os.environ.get("TEST_REDIS_URL", "redis://localhost:6379/15")
_DB_PATH = Path(tempfile.mkdtemp(prefix="quotaguard-tests-")) / "test.db"

os.environ["REDIS_URL"] = TEST_REDIS_URL
os.environ["DATABASE_URL"] = f"sqlite:///{_DB_PATH}"
os.environ["QG_ADMIN_TOKEN"] = ADMIN_TOKEN
os.environ.pop("QG_SERVICE_TOKEN", None)
os.environ["LOG_LEVEL"] = "WARNING"
# The production default of 100 ms is a latency budget, not a correctness bound; the
# concurrency tests fire 50 commands at once and must not flake on a loaded runner.
os.environ["REDIS_TIMEOUT_MS"] = "5000"

import httpx  # noqa: E402
import pytest  # noqa: E402
import redis.asyncio as redis_asyncio  # noqa: E402
from alembic import command  # noqa: E402
from alembic.config import Config  # noqa: E402

from app.config import get_settings  # noqa: E402

get_settings.cache_clear()

from app import redis_client as app_redis  # noqa: E402
from app.db import SessionLocal, engine  # noqa: E402
from app.main import app  # noqa: E402
from app.services import keycache  # noqa: E402

PROJECT_ROOT = Path(__file__).resolve().parent.parent
ADMIN_HEADERS = {"X-Admin-Token": ADMIN_TOKEN}

PLAN_PAYLOAD = {
    "slug": "pro",
    "name": "Pro",
    "burst_capacity": 100,
    "burst_refill_per_sec": 50,
    "sustained_limit": 5000,
    "sustained_window_seconds": 3600,
    "monthly_quota": 500000,
    "quota_soft_pct": 80,
    "webhook_url": "https://ops.example.com/hooks/quota",
}


@pytest.fixture(scope="session", autouse=True)
def _migrated_database():
    config = Config(str(PROJECT_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(PROJECT_ROOT / "migrations"))
    command.upgrade(config, "head")
    yield
    engine.dispose()


@pytest.fixture(autouse=True)
def _clean_tables(_migrated_database):
    """Every test starts from an empty database; children first for the foreign key."""
    from app.models import ApiKey, Plan

    session = SessionLocal()
    try:
        session.query(ApiKey).delete()
        session.query(Plan).delete()
        session.commit()
    finally:
        session.close()
    keycache.invalidate()


@pytest.fixture
def db():
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture
async def client():
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as http_client:
        yield http_client


@pytest.fixture
async def redis_client():
    connection = redis_asyncio.from_url(TEST_REDIS_URL, decode_responses=True)
    await connection.flushdb()
    yield connection
    await connection.flushdb()
    await connection.aclose()


@pytest.fixture
async def script():
    """The application's own script registry and pool, against a flushed database.

    The pool is rebuilt per test because it binds to the running event loop, and
    every test using the check path takes this fixture so the teardown closes it.
    """
    client = app_redis.get_client()
    await client.flushdb()
    registry = app_redis.get_registry()
    await registry.load_all()
    yield registry
    await client.flushdb()
    await app_redis.close()


@pytest.fixture
def resolved():
    """Build a ResolvedKey directly, for tests that drive the script without HTTP."""
    from app.services.keycache import ResolvedKey

    defaults = {
        "key_id": "k_testkey0001",
        "name": "test key",
        "plan_slug": "pro",
        "revoked": False,
        "burst_capacity": 5,
        "burst_refill_per_sec": 1,
        "sustained_limit": 100,
        "sustained_window_seconds": 60,
        "monthly_quota": 1000,
        "quota_soft_pct": 0,
        "redis_down_policy": "fail_open",
    }

    def _resolved(**overrides):
        return ResolvedKey(**{**defaults, **overrides})

    return _resolved


@pytest.fixture
def do_check(client):
    """Post one valid check and return the decision body."""

    async def _do_check(api_key: str, resource: str = "search", cost: int | None = None):
        payload: dict[str, object] = {"api_key": api_key, "resource": resource}
        if cost is not None:
            payload["cost"] = cost
        response = await client.post("/v1/check", json=payload)
        assert response.status_code == 200, response.text
        return response.json()["data"]

    return _do_check


@pytest.fixture
def make_plan(client):
    async def _make_plan(**overrides):
        payload = {**PLAN_PAYLOAD, **overrides}
        response = await client.post("/admin/plans", json=payload, headers=ADMIN_HEADERS)
        assert response.status_code == 201, response.text
        return response.json()["data"]

    return _make_plan


@pytest.fixture
def make_key(client):
    async def _make_key(plan: str = "pro", name: str = "acme production"):
        response = await client.post(
            "/admin/keys", json={"plan": plan, "name": name}, headers=ADMIN_HEADERS
        )
        assert response.status_code == 201, response.text
        return response.json()["data"]

    return _make_key
