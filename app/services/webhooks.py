"""The soft-threshold webhook outbox.

The check path enqueues a row when the script reports the crossing; the unique
constraint on (api_key_id, month, kind) is what makes the warning at-most-once
per key per month, so an enqueue that loses the race is a no-op, not an error.
An enqueue failure is logged and swallowed: a warning is never worth failing an
admission decision for.
"""

import hashlib
import hmac
import json
import logging
import random
from datetime import datetime, timedelta, UTC

import httpx
from sqlalchemy import and_, select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import SessionLocal
from app.models import ApiKey, Plan, WebhookOutbox, to_iso_utc, utcnow
from app.services.keycache import ResolvedKey

logger = logging.getLogger("quotaguard.webhooks")

EVENT = "quota.soft_threshold"
KIND = "quota_soft"
USER_AGENT = "quotaguard/1.0"
SIGNATURE_HEADER = "X-QuotaGuard-Signature"

BASE_BACKOFF_SECONDS = 30
MAX_BACKOFF_SECONDS = 3600
# Doubling stops here; WEBHOOK_MAX_ATTEMPTS caps at 50 and 2^17 already exceeds the cap.
MAX_DOUBLINGS = 17
# Rows handled per delivery pass, so one pass cannot run unbounded.
BATCH_SIZE = 50


def build_payload(
    key_id: str,
    key_name: str,
    plan_slug: str,
    month: str,
    used: int,
    quota: int,
    threshold_pct: int,
) -> str:
    """The exact JSON body that will be POSTed, frozen at enqueue time."""
    return json.dumps(
        {
            "event": EVENT,
            "key_id": key_id,
            "key_name": key_name,
            "plan": plan_slug,
            "month": month,
            "used": used,
            "quota": quota,
            "threshold_pct": threshold_pct,
            "sent_at": to_iso_utc(utcnow()),
        }
    )


def enqueue(db: Session, api_key_id: int, month: str, payload: str) -> bool:
    """Insert one outbox row. False means the row already existed."""
    db.add(
        WebhookOutbox(
            api_key_id=api_key_id,
            month=month,
            kind=KIND,
            payload=payload,
            next_attempt_at=utcnow(),
        )
    )
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        return False
    return True


def enqueue_soft_warning(key: ResolvedKey, month: str, used: int) -> bool:
    """Called off the check path; every failure here is logged, never raised."""
    session = SessionLocal()
    try:
        api_key_id = session.scalar(select(ApiKey.id).where(ApiKey.key_id == key.key_id))
        if api_key_id is None:
            return False
        payload = build_payload(
            key_id=key.key_id,
            key_name=key.name,
            plan_slug=key.plan_slug,
            month=month,
            used=used,
            quota=key.monthly_quota,
            threshold_pct=key.quota_soft_pct,
        )
        return enqueue(session, api_key_id, month, payload)
    except SQLAlchemyError as exc:
        session.rollback()
        logger.exception(
            "soft threshold warning could not be enqueued: %s",
            exc.__class__.__name__,
            extra={"key_id": key.key_id, "month": month},
        )
        return False
    finally:
        session.close()


def compute_next_attempt(attempt_num: int) -> datetime:
    """Backoff schedule: min(30 * 2^(n-1), 3600) seconds with 0.8-1.2 jitter."""
    exponent = min(attempt_num - 1, MAX_DOUBLINGS)
    base_seconds = min(BASE_BACKOFF_SECONDS * (2 ** exponent), MAX_BACKOFF_SECONDS)
    jitter = random.uniform(0.8, 1.2)
    delay_seconds = base_seconds * jitter
    return utcnow() + timedelta(seconds=delay_seconds)


def sign_payload(payload: str) -> str:
    """Compute HMAC-SHA256 signature over the payload body when secret is set."""
    settings = get_settings()
    if not settings.qg_webhook_secret:
        return ""
    sig = hmac.new(
        settings.qg_webhook_secret.encode(),
        payload.encode(),
        hashlib.sha256,
    ).hexdigest()
    return f"sha256={sig}"


def deliver_one(db: Session, row: WebhookOutbox, plan: Plan) -> bool:
    """POST the webhook and update the row. Returns True on success (2xx), False on failure."""
    settings = get_settings()
    signature = sign_payload(row.payload)
    headers = {
        "User-Agent": USER_AGENT,
        "X-QuotaGuard-Event": EVENT,
    }
    if signature:
        headers[SIGNATURE_HEADER] = signature

    try:
        with httpx.Client() as client:
            resp = client.post(
                plan.webhook_url,
                content=row.payload,
                headers=headers,
                timeout=settings.webhook_timeout_seconds,
            )
        if 200 <= resp.status_code < 300:
            row.delivered_at = utcnow()
            row.last_error = None
            db.commit()
            logger.info(
                "webhook delivered",
                extra={
                    "key_id": db.scalar(select(ApiKey.key_id).where(ApiKey.id == row.api_key_id)),
                    "month": row.month,
                    "status": resp.status_code,
                },
            )
            return True
        else:
            row.attempts += 1
            row.last_error = f"HTTP {resp.status_code}"
            row.next_attempt_at = compute_next_attempt(row.attempts)
            db.commit()
            return False
    except (httpx.RequestError, httpx.TimeoutException) as exc:
        row.attempts += 1
        row.last_error = exc.__class__.__name__
        row.next_attempt_at = compute_next_attempt(row.attempts)
        db.commit()
        return False


def deliver_webhooks() -> int:
    """Process due outbox rows and deliver them. Returns count of rows processed."""
    settings = get_settings()
    session = SessionLocal()
    processed = 0

    try:
        now = utcnow()
        due_rows = session.query(WebhookOutbox).filter(
            and_(
                WebhookOutbox.delivered_at.is_(None),
                WebhookOutbox.next_attempt_at <= now,
                WebhookOutbox.attempts < settings.webhook_max_attempts,
            )
        ).limit(BATCH_SIZE).all()

        for row in due_rows:
            api_key = session.scalar(select(ApiKey).where(ApiKey.id == row.api_key_id))
            if api_key is None:
                logger.warning("webhook row has no api_key", extra={"row_id": row.id})
                continue
            plan = session.scalar(select(Plan).where(Plan.id == api_key.plan_id))
            if plan is None or not plan.webhook_url:
                logger.warning("api_key has no plan or webhook_url", extra={"api_key_id": api_key.key_id})
                continue

            deliver_one(session, row, plan)
            processed += 1

        # Check for exhausted rows and log them.
        exhausted = session.query(WebhookOutbox).filter(
            and_(
                WebhookOutbox.delivered_at.is_(None),
                WebhookOutbox.attempts >= settings.webhook_max_attempts,
            )
        ).all()

        for row in exhausted:
            key_id = session.scalar(select(ApiKey.key_id).where(ApiKey.id == row.api_key_id))
            logger.warning(
                "webhook.exhausted",
                extra={
                    "key_id": key_id,
                    "month": row.month,
                    "attempts": row.attempts,
                    "last_error": row.last_error,
                },
            )

    except SQLAlchemyError as exc:
        session.rollback()
        logger.exception("webhook delivery loop error: %s", exc.__class__.__name__)
    finally:
        session.close()

    return processed
