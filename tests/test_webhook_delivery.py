"""Webhook delivery: signing, backoff, and exhaustion logged exactly once."""

import hashlib
import hmac
import logging
from datetime import UTC, timedelta

import httpx
import pytest
from sqlalchemy import select

from app.config import get_settings
from app.models import WebhookOutbox, utcnow
from app.schemas.plans import PlanCreateRequest
from app.services import keys as keys_service
from app.services import plans as plans_service
from app.services import webhooks


@pytest.fixture
def outbox_row(db):
    plans_service.create_plan(
        db,
        PlanCreateRequest(
            slug="trial",
            name="Trial",
            burst_capacity=10,
            burst_refill_per_sec=5,
            sustained_limit=100,
            sustained_window_seconds=60,
            monthly_quota=10,
            quota_soft_pct=80,
            webhook_url="http://hooks.test/quota",
        ),
    )
    key, _ = keys_service.issue_key(db, "trial", "acme")
    db.add(
        WebhookOutbox(
            api_key_id=key.id,
            month="2026-11",
            kind="quota_soft",
            payload='{"event": "quota.soft_threshold"}',
            next_attempt_at=utcnow() - timedelta(seconds=1),
        )
    )
    db.commit()


@pytest.fixture
def endpoint(monkeypatch):
    """Route webhook POSTs to a scripted handler; returns the list of received requests."""
    received: list[httpx.Request] = []
    replies: list[int] = []
    real_client = httpx.Client

    def handler(request: httpx.Request) -> httpx.Response:
        received.append(request)
        return httpx.Response(replies.pop(0) if replies else 204)

    monkeypatch.setattr(
        webhooks.httpx, "Client", lambda **kw: real_client(transport=httpx.MockTransport(handler))
    )
    return received, replies


def stored(db) -> WebhookOutbox:
    db.expire_all()
    return db.scalar(select(WebhookOutbox))


def test_a_2xx_marks_the_row_delivered_with_a_valid_signature(
    db, outbox_row, endpoint, monkeypatch
):
    monkeypatch.setenv("QG_WEBHOOK_SECRET", "whsec")
    get_settings.cache_clear()
    received, _ = endpoint

    assert webhooks.deliver_webhooks() == 1
    assert webhooks.deliver_webhooks() == 0  # delivered rows are never sent again
    get_settings.cache_clear()

    request = received[0]
    expected = "sha256=" + hmac.new(b"whsec", request.content, hashlib.sha256).hexdigest()
    assert request.headers["X-QuotaGuard-Signature"] == expected
    assert request.headers["X-QuotaGuard-Event"] == "quota.soft_threshold"
    assert stored(db).delivered_at is not None


def test_failures_back_off_and_exhaustion_is_logged_once(
    db, outbox_row, endpoint, monkeypatch, caplog
):
    monkeypatch.setenv("WEBHOOK_MAX_ATTEMPTS", "2")
    get_settings.cache_clear()
    # Alembic's fileConfig (run by the session fixture) disables loggers that already exist.
    webhook_logger = logging.getLogger("quotaguard.webhooks")
    monkeypatch.setattr(webhook_logger, "disabled", False)
    _, replies = endpoint
    replies.extend([500, 503])

    with caplog.at_level(logging.WARNING):
        webhooks.deliver_webhooks()
        row = stored(db)
        delay = (row.next_attempt_at.replace(tzinfo=UTC) - utcnow()).total_seconds()
        assert (row.attempts, row.last_error) == (1, "HTTP 500")
        assert 30 * 0.8 - 2 <= delay <= 30 * 1.2

        row.next_attempt_at = utcnow() - timedelta(seconds=1)
        db.commit()
        webhooks.deliver_webhooks()
        webhooks.deliver_webhooks()  # exhausted rows are not retried or re-logged
    get_settings.cache_clear()

    assert stored(db).attempts == 2
    assert stored(db).delivered_at is None
    assert sum("webhook.exhausted" in r.getMessage() for r in caplog.records) == 1


def test_backoff_doubles_and_caps_at_an_hour(monkeypatch):
    monkeypatch.setattr(webhooks.random, "uniform", lambda a, b: 1.0)
    now = utcnow()
    delays = [
        round((webhooks.compute_next_attempt(n) - now).total_seconds()) for n in (1, 2, 3, 8, 20)
    ]
    assert delays == [30, 60, 120, 3600, 3600]
