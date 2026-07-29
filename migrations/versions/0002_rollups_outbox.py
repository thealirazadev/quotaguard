"""Create rollups and webhook_outbox.

Revision ID: 0002_rollups_outbox
Revises: 0001_init
"""

import sqlalchemy as sa
from alembic import op

revision: str = "0002_rollups_outbox"
down_revision: str | None = "0001_init"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "rollups",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("api_key_id", sa.Integer(), nullable=False),
        sa.Column("month", sa.String(length=7), nullable=False),
        sa.Column("used", sa.Integer(), nullable=False),
        sa.Column("quota_limit", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["api_key_id"], ["api_keys.id"], name="fk_rollups_api_key_id"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("api_key_id", "month", name="uq_rollups_api_key_month"),
    )
    op.create_index("ix_rollups_api_key_id", "rollups", ["api_key_id"], unique=False)

    op.create_table(
        "webhook_outbox",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("api_key_id", sa.Integer(), nullable=False),
        sa.Column("month", sa.String(length=7), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.String(length=512), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("kind IN ('quota_soft')", name="ck_webhook_outbox_kind"),
        sa.ForeignKeyConstraint(
            ["api_key_id"], ["api_keys.id"], name="fk_webhook_outbox_api_key_id"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "api_key_id", "month", "kind", name="uq_webhook_outbox_api_key_month_kind"
        ),
    )
    op.create_index("ix_webhook_outbox_api_key_id", "webhook_outbox", ["api_key_id"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_webhook_outbox_api_key_id", table_name="webhook_outbox")
    op.drop_table("webhook_outbox")
    op.drop_index("ix_rollups_api_key_id", table_name="rollups")
    op.drop_table("rollups")
