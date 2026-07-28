"""Shared FastAPI dependencies: database sessions and token authentication."""

import hmac
from collections.abc import Iterator
from typing import Annotated

from fastapi import Depends, Header
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.db import SessionLocal
from app.errors import UnauthorizedError

SettingsDep = Annotated[Settings, Depends(get_settings)]


def get_db() -> Iterator[Session]:
    session = SessionLocal()
    try:
        yield session
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def require_admin(
    settings: SettingsDep,
    x_admin_token: Annotated[str | None, Header()] = None,
) -> None:
    """Guards every /admin route and the usage report. Never logs the token."""
    if x_admin_token is None or not hmac.compare_digest(x_admin_token, settings.qg_admin_token):
        raise UnauthorizedError("A valid X-Admin-Token header is required.")


def require_service_token(
    settings: SettingsDep,
    x_service_token: Annotated[str | None, Header()] = None,
) -> None:
    """No-op unless QG_SERVICE_TOKEN is configured."""
    expected = settings.qg_service_token
    if not expected:
        return
    if x_service_token is None or not hmac.compare_digest(x_service_token, expected):
        raise UnauthorizedError("A valid X-Service-Token header is required.")


DbDep = Annotated[Session, Depends(get_db)]
AdminAuth = Depends(require_admin)
