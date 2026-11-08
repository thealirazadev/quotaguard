"""Usage report: current month from Redis, history from rollups."""

import logging
from datetime import datetime

from sqlalchemy import desc, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.models import ApiKey, Rollup, WebhookOutbox, utcnow
from app.redis_client import get_pool

logger = logging.getLogger("quotaguard.usage")


def month_from_timestamp(ts: datetime) -> str:
    """Convert a datetime to YYYY-MM format."""
    return ts.strftime("%Y-%m")


def get_current_month() -> str:
    """Get the current month in YYYY-MM format."""
    return month_from_timestamp(utcnow())


def get_live_usage(key_id: str) -> dict | None:
    """Get current month usage from Redis for a key.

    Returns month, used, soft_threshold_crossed, and source ("live"), or None if the key has no
    Redis counter this month.
    """
    pool = get_pool()
    current_month = get_current_month()

    try:
        quota_key = f"qg:q:{key_id}:{current_month}"
        flag_key = f"qg:f:{key_id}:{current_month}"

        used_str = pool.execute_command("GET", quota_key)
        if used_str is None:
            return None

        used = int(used_str)
        flag_set = pool.execute_command("GET", flag_key) is not None

        return {
            "month": current_month,
            "used": used,
            "soft_threshold_crossed": flag_set,
            "source": "live",
        }
    except Exception as exc:
        logger.exception("failed to fetch live usage: %s", exc.__class__.__name__)
        return None


def get_rollup_history(
    db: Session, api_key_id: int, months: int = 3, before: str | None = None
) -> list[dict]:
    """Rollup history for a key, most recent first, for months earlier than ``before``.

    soft_threshold_crossed comes from the webhook outbox: a row exists exactly when the soft
    threshold was crossed that month (the unique constraint makes it one per key per month).
    """
    try:
        query = db.query(Rollup).where(Rollup.api_key_id == api_key_id)
        if before is not None:
            query = query.where(Rollup.month < before)
        rollups = query.order_by(desc(Rollup.month)).limit(months).all()
        warned = {
            month
            for (month,) in db.query(WebhookOutbox.month).where(
                WebhookOutbox.api_key_id == api_key_id, WebhookOutbox.kind == "quota_soft"
            )
        }
        return [
            {
                "month": rollup.month,
                "used": rollup.used,
                "quota": rollup.quota_limit,
                "soft_threshold_crossed": rollup.month in warned,
                "source": "rollup",
            }
            for rollup in rollups
        ]
    except SQLAlchemyError as exc:
        logger.exception("failed to fetch rollup history: %s", exc.__class__.__name__)
        return []


def get_usage_report(db: Session, key_id: str, months: int = 3) -> dict:
    """Build the full usage report for a key.

    Returns a dict with key metadata and a list of months (current live month + prior rollups).
    months is 1-24; excess is silently ignored.
    """
    api_key = db.scalar(select(ApiKey).where(ApiKey.key_id == key_id))
    if api_key is None:
        return None

    # Build the response structure.
    report = {
        "key_id": api_key.key_id,
        "name": api_key.name,
        "plan": api_key.plan.slug,
        "months": [],
    }

    # Get live month first.
    live = get_live_usage(key_id)
    if live:
        live["quota"] = (
            api_key.override_monthly_quota
            if api_key.override_monthly_quota is not None
            else api_key.plan.monthly_quota
        )
        live["remaining"] = live["quota"] - live["used"]
        # The Redis flag is lost with Redis data; the outbox row is the durable record.
        live["soft_threshold_crossed"] = live["soft_threshold_crossed"] or (
            db.scalar(
                select(WebhookOutbox.id).where(
                    WebhookOutbox.api_key_id == api_key.id,
                    WebhookOutbox.month == live["month"],
                    WebhookOutbox.kind == "quota_soft",
                )
            )
            is not None
        )
        report["months"].append(live)

    elif (current := get_rollup_history(db, api_key.id, 1)) and current[0]["month"] == (
        get_current_month()
    ):
        # No live counter (Redis lost it and no rollup has restored it yet): report the
        # persisted value rather than hide the month.
        current[0]["remaining"] = current[0]["quota"] - current[0]["used"]
        report["months"].append(current[0])

    # Then earlier months from rollups.
    slots = months - len(report["months"])
    history = get_rollup_history(db, api_key.id, slots, before=get_current_month()) if slots else []
    for month_data in history:
        month_data["remaining"] = month_data["quota"] - month_data["used"]
        report["months"].append(month_data)

    return report
