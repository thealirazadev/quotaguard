"""Operator routes for plans and keys. Every route requires X-Admin-Token."""

from fastapi import APIRouter, Depends, status

from app.deps import DbDep, require_admin
from app.models import ApiKey
from app.schemas.keys import (
    KeyDetailOut,
    KeyIssuedOut,
    KeyIssueRequest,
    KeyListOut,
    KeyOut,
    KeyRevokedOut,
    KeyUpdateRequest,
)
from app.schemas.plans import (
    PlanCreateRequest,
    PlanDetailOut,
    PlanListOut,
    PlanOut,
    PlanUpdateRequest,
)
from app.services import keys as keys_service
from app.services import plans as plans_service

router = APIRouter(prefix="/admin", tags=["admin"], dependencies=[Depends(require_admin)])


def _key_out(key: ApiKey) -> KeyOut:
    return KeyOut(
        key_id=key.key_id,
        key_prefix=key.key_prefix,
        name=key.name,
        plan=key.plan.slug,
        overrides=key.overrides(),
        revoked_at=key.revoked_at,
        created_at=key.created_at,
    )


@router.post("/plans", status_code=status.HTTP_201_CREATED)
def create_plan(payload: PlanCreateRequest, db: DbDep) -> dict:
    plan = plans_service.create_plan(db, payload)
    return {"data": PlanOut.model_validate(plan)}


@router.get("/plans")
def list_plans(db: DbDep) -> dict:
    records = plans_service.list_plans(db)
    body = PlanListOut(
        plans=[PlanOut.model_validate(record) for record in records], total=len(records)
    )
    return {"data": body}


@router.get("/plans/{slug}")
def get_plan(slug: str, db: DbDep) -> dict:
    plan = plans_service.get_plan(db, slug)
    fields = PlanOut.model_validate(plan).model_dump()
    fields["keys_count"] = plans_service.count_keys(db, plan)
    return {"data": PlanDetailOut.model_validate(fields)}


@router.patch("/plans/{slug}")
def update_plan(slug: str, payload: PlanUpdateRequest, db: DbDep) -> dict:
    plan = plans_service.update_plan(db, slug, payload)
    return {"data": PlanOut.model_validate(plan)}


@router.post("/keys", status_code=status.HTTP_201_CREATED)
def issue_key(payload: KeyIssueRequest, db: DbDep) -> dict:
    """The only response that ever carries the plaintext secret."""
    key, secret = keys_service.issue_key(db, payload.plan, payload.name)
    issued = KeyIssuedOut(
        key_id=key.key_id,
        name=key.name,
        plan=key.plan.slug,
        api_key=secret,
        key_prefix=key.key_prefix,
        created_at=key.created_at,
    )
    return {"data": issued}


@router.get("/keys")
def list_keys(db: DbDep, plan: str | None = None) -> dict:
    records = keys_service.list_keys(db, plan)
    body = KeyListOut(keys=[_key_out(record) for record in records], total=len(records))
    return {"data": body}


@router.get("/keys/{key_id}")
def get_key(key_id: str, db: DbDep) -> dict:
    key = keys_service.get_key(db, key_id)
    detail = KeyDetailOut(**_key_out(key).model_dump(), effective_limits=key.effective_limits())
    return {"data": detail}


@router.patch("/keys/{key_id}")
def update_key(key_id: str, payload: KeyUpdateRequest, db: DbDep) -> dict:
    key = keys_service.update_key(db, key_id, payload)
    detail = KeyDetailOut(**_key_out(key).model_dump(), effective_limits=key.effective_limits())
    return {"data": detail}


@router.post("/keys/{key_id}/revoke")
def revoke_key(key_id: str, db: DbDep) -> dict:
    key = keys_service.revoke_key(db, key_id)
    return {"data": KeyRevokedOut(key_id=key.key_id, revoked_at=key.revoked_at)}
