"""Usage report: the current month live from Redis, earlier months from rollups."""

from typing import Annotated

from fastapi import APIRouter, Depends, Query

from app.deps import DbDep, require_admin
from app.errors import NotFoundError
from app.schemas.usage import UsageReport
from app.services import usage as usage_service

router = APIRouter(prefix="/v1", tags=["usage"], dependencies=[Depends(require_admin)])


@router.get("/usage/{key_id}")
def get_usage(key_id: str, db: DbDep, months: Annotated[int, Query(ge=1, le=24)] = 3) -> dict:
    report = usage_service.get_usage_report(db, key_id, months)
    if report is None:
        raise NotFoundError(f"No key with id {key_id}.")
    return {"data": UsageReport(**report)}
