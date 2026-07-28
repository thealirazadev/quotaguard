"""Plan CRUD. Bound checks live in the schemas; cross-field rules live here."""

import logging

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.errors import ConflictError, NotFoundError, ValidationError
from app.models import ApiKey, Plan
from app.schemas.plans import PlanCreateRequest, PlanUpdateRequest

logger = logging.getLogger("quotaguard.plans")


def _require_webhook_for_soft_threshold(quota_soft_pct: int, webhook_url: str | None) -> None:
    if quota_soft_pct > 0 and not webhook_url:
        raise ValidationError("webhook_url is required when quota_soft_pct is greater than 0.")


def create_plan(db: Session, data: PlanCreateRequest) -> Plan:
    _require_webhook_for_soft_threshold(data.quota_soft_pct, data.webhook_url)
    plan = Plan(**data.model_dump())
    db.add(plan)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise ConflictError(f"A plan with slug '{data.slug}' already exists.") from exc
    db.refresh(plan)
    logger.info("plan created", extra={"event": "plan.created", "policy": plan.redis_down_policy})
    return plan


def list_plans(db: Session) -> list[Plan]:
    return list(db.scalars(select(Plan).order_by(Plan.slug)).all())


def get_plan(db: Session, slug: str) -> Plan:
    plan = db.scalar(select(Plan).where(Plan.slug == slug))
    if plan is None:
        raise NotFoundError(f"No plan with slug '{slug}' exists.")
    return plan


def count_keys(db: Session, plan: Plan) -> int:
    return db.scalar(select(func.count()).select_from(ApiKey).where(ApiKey.plan_id == plan.id)) or 0


def update_plan(db: Session, slug: str, data: PlanUpdateRequest) -> Plan:
    plan = get_plan(db, slug)
    changes = data.model_dump(exclude_unset=True)
    for field, value in changes.items():
        # Only webhook_url is nullable; an explicit null anywhere else is a client mistake.
        if value is None and field != "webhook_url":
            raise ValidationError(f"{field} cannot be set to null.")
    merged_soft_pct = changes.get("quota_soft_pct", plan.quota_soft_pct)
    merged_webhook = changes.get("webhook_url", plan.webhook_url)
    _require_webhook_for_soft_threshold(merged_soft_pct, merged_webhook)
    for field, value in changes.items():
        setattr(plan, field, value)
    db.commit()
    db.refresh(plan)
    logger.info("plan updated", extra={"event": "plan.updated", "policy": plan.redis_down_policy})
    return plan
