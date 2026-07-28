"""Api key issue and lookup.

The plaintext secret is generated here, returned once, and never stored: only
its sha256 hash (the check-path lookup index) and a display prefix persist.
"""

import hashlib
import logging
import secrets

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.errors import NotFoundError
from app.models import ApiKey
from app.services import plans as plans_service

logger = logging.getLogger("quotaguard.keys")

SECRET_PREFIX = "qk_"
KEY_ID_PREFIX = "k_"
DISPLAY_PREFIX_LENGTH = 12


def hash_secret(secret: str) -> str:
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def generate_secret() -> str:
    """`qk_` plus 43 url-safe characters from 32 random bytes."""
    return SECRET_PREFIX + secrets.token_urlsafe(32)


def generate_key_id() -> str:
    return KEY_ID_PREFIX + secrets.token_hex(6)


def issue_key(db: Session, plan_slug: str, name: str) -> tuple[ApiKey, str]:
    plan = plans_service.get_plan(db, plan_slug)
    secret = generate_secret()
    key = ApiKey(
        key_id=generate_key_id(),
        key_hash=hash_secret(secret),
        key_prefix=secret[:DISPLAY_PREFIX_LENGTH],
        name=name,
        plan_id=plan.id,
    )
    db.add(key)
    db.commit()
    db.refresh(key)
    logger.info("key issued", extra={"event": "key.issued", "key_id": key.key_id})
    return key, secret


def list_keys(db: Session, plan_slug: str | None = None) -> list[ApiKey]:
    statement = select(ApiKey).order_by(ApiKey.created_at, ApiKey.id)
    if plan_slug is not None:
        plan = plans_service.get_plan(db, plan_slug)
        statement = statement.where(ApiKey.plan_id == plan.id)
    return list(db.scalars(statement).all())


def get_key(db: Session, key_id: str) -> ApiKey:
    key = db.scalar(select(ApiKey).where(ApiKey.key_id == key_id))
    if key is None:
        raise NotFoundError(f"No key with id '{key_id}' exists.")
    return key
