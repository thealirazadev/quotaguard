"""Create plans and api_keys.

Revision ID: 0001_init
Revises:
"""

import sqlalchemy as sa
from alembic import op

revision: str = "0001_init"
down_revision: str | None = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "plans",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("slug", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("burst_capacity", sa.Integer(), nullable=False),
        sa.Column("burst_refill_per_sec", sa.Integer(), nullable=False),
        sa.Column("sustained_limit", sa.Integer(), nullable=False),
        sa.Column("sustained_window_seconds", sa.Integer(), nullable=False),
        sa.Column("monthly_quota", sa.Integer(), nullable=False),
        sa.Column("quota_soft_pct", sa.Integer(), nullable=False),
        sa.Column("webhook_url", sa.String(length=2048), nullable=True),
        sa.Column(
            "redis_down_policy",
            sa.String(length=16),
            server_default="fail_open",
            nullable=False,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint(
            "redis_down_policy IN ('fail_open', 'fail_closed')",
            name="ck_plans_redis_down_policy",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_plans_slug", "plans", ["slug"], unique=True)

    op.create_table(
        "api_keys",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("key_id", sa.String(length=32), nullable=False),
        sa.Column("key_hash", sa.String(length=64), nullable=False),
        sa.Column("key_prefix", sa.String(length=16), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("plan_id", sa.Integer(), nullable=False),
        sa.Column("override_burst_capacity", sa.Integer(), nullable=True),
        sa.Column("override_burst_refill_per_sec", sa.Integer(), nullable=True),
        sa.Column("override_sustained_limit", sa.Integer(), nullable=True),
        sa.Column("override_sustained_window_seconds", sa.Integer(), nullable=True),
        sa.Column("override_monthly_quota", sa.Integer(), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["plan_id"], ["plans.id"], name="fk_api_keys_plan_id"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_api_keys_key_id", "api_keys", ["key_id"], unique=True)
    op.create_index("ix_api_keys_key_hash", "api_keys", ["key_hash"], unique=True)
    op.create_index("ix_api_keys_plan_id", "api_keys", ["plan_id"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_api_keys_plan_id", table_name="api_keys")
    op.drop_index("ix_api_keys_key_hash", table_name="api_keys")
    op.drop_index("ix_api_keys_key_id", table_name="api_keys")
    op.drop_table("api_keys")
    op.drop_index("ix_plans_slug", table_name="plans")
    op.drop_table("plans")
