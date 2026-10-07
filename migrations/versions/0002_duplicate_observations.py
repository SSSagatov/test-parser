"""Persist invalid duplicate observations separately from canonical lots."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0002_duplicate_observations"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "duplicate_observations",
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("source_id", sa.String(length=255), nullable=False),
        sa.Column("is_valid", sa.Boolean(), nullable=False),
        sa.Column("reason", sa.String(length=64), nullable=False),
        sa.Column("data", sa.JSON().with_variant(postgresql.JSONB(), "postgresql"), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["run_id"], ["crawl_runs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("run_id", "source_id"),
    )
    op.create_index("ix_duplicate_observations_run_id", "duplicate_observations", ["run_id"])


def downgrade():
    op.drop_index("ix_duplicate_observations_run_id", table_name="duplicate_observations")
    op.drop_table("duplicate_observations")
