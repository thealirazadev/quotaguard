"""Webhook delivery against a local httpx MockTransport."""

import logging

import httpx
import pytest

from app.config import get_settings
from app.db import SessionLocal
from app.models import ApiKey, WebhookOutbox
from app.services import webhooks


@pytest.fixture(autouse=True)
def _clean_outbox():
    yield
    session = SessionLocal()
    try:
        session.query(WebhookOutbox).delete()
        session.commit()
    finally:
        session.close()


@pytest.fixture
def receiver(monkeypatch):
    """Route every delivery to a handler answering with the queued status codes."""
    statuses: list[int] = []
    received: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        received.append(request)
        return httpx.Response(statuses.pop(0) if statuses else 200)

    real_client = httpx.Client
    monkeypatch.setattr(
        webhooks.httpx, "Client", lambda: real_client(transport=httpx.MockTransport(handler))
    )
    return statuses, received


@pytest.fixture
async def outbox_row(make_plan, make_key):
    await make_plan()
    key_id = (await make_key())["key_id"]
    session = SessionLocal()
    try:
        api_key_id = session.query(ApiKey.id).filter(ApiKey.key_id == key_id).scalar()
        assert webhooks.enqueue(session, api_key_id, "2026-07", '{"event": "x"}')
        return session.query(WebhookOutbox.id).scalar()
    finally:
        session.close()


def _make_due(row_id: int, attempts: int) -> None:
    session = SessionLocal()
    try:
        row = session.get(WebhookOutbox, row_id)
        row.attempts = attempts
        row.next_attempt_at = webhooks.utcnow()
        session.commit()
    finally:
        session.close()


def test_exhaustion_is_logged_once_at_error(outbox_row, receiver, caplog):
    statuses, received = receiver
    cap = get_settings().webhook_max_attempts
    _make_due(outbox_row, cap - 1)
    statuses.append(500)

    with caplog.at_level(logging.WARNING, logger="quotaguard.webhooks"):
        assert webhooks.deliver_webhooks() == 1
        assert webhooks.deliver_webhooks() == 0
        assert webhooks.deliver_webhooks() == 0

    exhausted = [r for r in caplog.records if getattr(r, "event", None) == "webhook.exhausted"]
    assert len(exhausted) == 1
    assert exhausted[0].levelno == logging.ERROR
    assert len(received) == 1


def test_failure_below_the_cap_is_a_retry(outbox_row, receiver, caplog):
    statuses, _ = receiver
    statuses.append(503)

    with caplog.at_level(logging.WARNING, logger="quotaguard.webhooks"):
        webhooks.deliver_webhooks()

    events = [getattr(r, "event", None) for r in caplog.records]
    assert events == ["webhook.retry"]
    session = SessionLocal()
    try:
        row = session.get(WebhookOutbox, outbox_row)
        assert row.attempts == 1
        assert row.last_error == "HTTP 503"
        assert row.delivered_at is None
    finally:
        session.close()
