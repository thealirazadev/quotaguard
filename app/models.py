"""SQLAlchemy models for configuration and history."""

from datetime import UTC, datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Integer, String, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

REDIS_DOWN_POLICIES = ("fail_open", "fail_closed")

OVERRIDE_TO_PLAN_FIELD = {
    "override_burst_capacity": "burst_capacity",
    "override_burst_refill_per_sec": "burst_refill_per_sec",
    "override_sustained_limit": "sustained_limit",
    "override_sustained_window_seconds": "sustained_window_seconds",
    "override_monthly_quota": "monthly_quota",
}


def utcnow() -> datetime:
    return datetime.now(UTC)


def to_iso_utc(value: datetime) -> str:
    """Render a stored timestamp as ISO-8601 UTC with a Z suffix.

    SQLite returns naive datetimes for DateTime(timezone=True) columns, so a
    missing tzinfo is treated as UTC rather than as local time.
    """
    aware = value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    return aware.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


class Base(DeclarativeBase):
    """Common surrogate key and audit columns for every table."""

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utcnow,
        onupdate=utcnow,
        server_default=func.now(),
    )


class Plan(Base):
    __tablename__ = "plans"
    __table_args__ = (
        CheckConstraint(
            "redis_down_policy IN ('fail_open', 'fail_closed')", name="ck_plans_redis_down_policy"
        ),
    )

    slug: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    burst_capacity: Mapped[int] = mapped_column(Integer, nullable=False)
    burst_refill_per_sec: Mapped[int] = mapped_column(Integer, nullable=False)
    sustained_limit: Mapped[int] = mapped_column(Integer, nullable=False)
    sustained_window_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    monthly_quota: Mapped[int] = mapped_column(Integer, nullable=False)
    quota_soft_pct: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    webhook_url: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    redis_down_policy: Mapped[str] = mapped_column(
        String(16), nullable=False, default="fail_open", server_default="fail_open"
    )

    keys: Mapped[list["ApiKey"]] = relationship(back_populates="plan")


class ApiKey(Base):
    __tablename__ = "api_keys"

    key_id: Mapped[str] = mapped_column(String(32), nullable=False, unique=True, index=True)
    key_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    key_prefix: Mapped[str] = mapped_column(String(16), nullable=False)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    plan_id: Mapped[int] = mapped_column(
        ForeignKey("plans.id", name="fk_api_keys_plan_id"), nullable=False, index=True
    )
    override_burst_capacity: Mapped[int | None] = mapped_column(Integer, nullable=True)
    override_burst_refill_per_sec: Mapped[int | None] = mapped_column(Integer, nullable=True)
    override_sustained_limit: Mapped[int | None] = mapped_column(Integer, nullable=True)
    override_sustained_window_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    override_monthly_quota: Mapped[int | None] = mapped_column(Integer, nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    plan: Mapped[Plan] = relationship(back_populates="keys")

    def effective_limits(self) -> dict[str, int]:
        """Override when set, else the plan value; resolved for reports and the key cache."""
        return {
            plan_field: getattr(self, override_field) or getattr(self.plan, plan_field)
            for override_field, plan_field in OVERRIDE_TO_PLAN_FIELD.items()
        }

    def overrides(self) -> dict[str, int]:
        return {
            override_field: getattr(self, override_field)
            for override_field in OVERRIDE_TO_PLAN_FIELD
            if getattr(self, override_field) is not None
        }
