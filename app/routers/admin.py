"""Operator routes for plans and keys. Every route requires X-Admin-Token."""

from fastapi import APIRouter, Depends, status

from app.deps import DbDep, require_admin
from app.schemas.plans import (
    PlanCreateRequest,
    PlanDetailOut,
    PlanListOut,
    PlanOut,
    PlanUpdateRequest,
)
from app.services import plans as plans_service

router = APIRouter(prefix="/admin", tags=["admin"], dependencies=[Depends(require_admin)])


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
