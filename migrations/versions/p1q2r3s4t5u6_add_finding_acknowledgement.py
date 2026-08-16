"""add acknowledgement columns to validation_finding (US-010)

PRD §7.3: warnings must be acknowledged before distribution and the
acknowledgement is logged. It is metadata, not a status — the finding
stays open (PRD §5 status enum is open|resolved|waived).

Revision ID: p1q2r3s4t5u6
Revises: o0p1q2r3s4t5
Create Date: 2026-06-12 00:00:00.000000

"""

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "p1q2r3s4t5u6"
down_revision = "o0p1q2r3s4t5"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "validation_finding",
        sa.Column("acknowledged_by", sa.Integer(), sa.ForeignKey("User.id"), nullable=True),
    )
    op.add_column(
        "validation_finding",
        sa.Column("acknowledged_at", sa.DateTime(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("validation_finding", "acknowledged_at")
    op.drop_column("validation_finding", "acknowledged_by")
