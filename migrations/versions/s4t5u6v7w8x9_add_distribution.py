"""add distribution table (ingestion PRD Stage C — gated publish to portals)

Revision ID: s4t5u6v7w8x9
Revises: r3s4t5u6v7w8
Create Date: 2026-07-06 00:00:00.000000

"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = "s4t5u6v7w8x9"
down_revision = "r3s4t5u6v7w8"
branch_labels = None
depends_on = None

catalog_enum = postgresql.ENUM("MECH", "YT", "PERF", name="catalog", create_type=False)


def upgrade() -> None:
    op.create_table(
        "distribution",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("statement_id", sa.Integer(), sa.ForeignKey("statement.id"), nullable=False),
        sa.Column("writer_id", sa.Integer(), sa.ForeignKey("writer.id"), nullable=False),
        sa.Column("batch_id", sa.Integer(), sa.ForeignKey("statement_batch.id"), nullable=False),
        sa.Column("period_code", sa.String(), nullable=False),
        sa.Column("catalog", catalog_enum, nullable=False),
        sa.Column("published_at", sa.DateTime(), nullable=False),
        sa.Column("published_by", sa.Integer(), sa.ForeignKey("User.id"), nullable=True),
        sa.Column("portal_visible", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("superseded_by", sa.Integer(), sa.ForeignKey("distribution.id"), nullable=True),
        sa.Column("gate_snapshot", sa.JSON(), nullable=True),
    )
    op.create_index("ix_distribution_statement_id", "distribution", ["statement_id"])
    op.create_index("ix_distribution_writer_id", "distribution", ["writer_id"])
    op.create_index("ix_distribution_batch_id", "distribution", ["batch_id"])
    op.create_index("ix_distribution_period_code", "distribution", ["period_code"])
    op.create_index("ix_distribution_portal_visible", "distribution", ["portal_visible"])


def downgrade() -> None:
    for ix in (
        "ix_distribution_portal_visible",
        "ix_distribution_period_code",
        "ix_distribution_batch_id",
        "ix_distribution_writer_id",
        "ix_distribution_statement_id",
    ):
        op.drop_index(ix, table_name="distribution")
    op.drop_table("distribution")
