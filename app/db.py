"""Engine and session factory built from DATABASE_URL."""

from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.orm import Session, sessionmaker

from app.config import get_settings


def _ensure_sqlite_directory(url: str) -> None:
    """SQLite will not create a missing parent directory for its file."""
    parsed = make_url(url)
    if not parsed.drivername.startswith("sqlite") or not parsed.database:
        return
    if parsed.database == ":memory:":
        return
    Path(parsed.database).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)


def build_engine(url: str) -> Engine:
    _ensure_sqlite_directory(url)
    connect_args = {"check_same_thread": False} if url.startswith("sqlite") else {}
    return create_engine(url, connect_args=connect_args, pool_pre_ping=True, future=True)


engine: Engine = build_engine(get_settings().database_url)
SessionLocal = sessionmaker(bind=engine, class_=Session, autoflush=False, expire_on_commit=False)
