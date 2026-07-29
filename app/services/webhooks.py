"""The soft-threshold webhook outbox.

The check path enqueues a row when the script reports the crossing; the unique
constraint on (api_key_id, month, kind) is what makes the warning at-most-once
per key per month, so an enqueue that loses the race is a no-op, not an error.
An enqueue failure is logged and swallowed: a warning is never worth failing an
admission decision for.
"""

import json
import logging

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from app.db import SessionLocal
from app.models import ApiKey, WebhookOutbox, to_iso_utc, utcnow
from app.services.keycache import ResolvedKey

logger = logging.getLogger("quotaguard.webhooks")

EVENT = "quota.soft_threshold"
KIND = "quota_soft"


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
