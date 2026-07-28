"""Liveness endpoint. Always HTTP 200 so a dependency outage never removes the process."""

from fastapi import APIRouter

router = APIRouter(tags=["health"])


@router.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}
