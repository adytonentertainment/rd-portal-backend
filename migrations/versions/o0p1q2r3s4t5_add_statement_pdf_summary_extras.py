"""add pdf summary extra columns to statement (US-007 parse stage)

The PDF parser extracts four official figures that PRD §5's column list
omits but later validation rules need: before_tax, payable_this,
carried_forward_out (V-STMT-3 payable identity for below-threshold
statements) and cheque_amount (V-STMT-6).

Revision ID: o0p1q2r3s4t5
Revises: n9o0p1q2r3s4
Create Date: 2026-06-11 00:00:00.000000

"""

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "o0p1q2r3s4t5"
down_revision = "n9o0p1q2r3s4"
branch_labels = None
depends_on = None

COLUMNS = ("before_tax", "payable_this", "carried_forward_out", "cheque_amount")


def upgrade() -> None:
    for name in COLUMNS:
        op.add_column("statement", sa.Column(name, sa.Numeric(14, 6), nullable=True))


def downgrade() -> None:
    for name in reversed(COLUMNS):
        op.drop_column("statement", name)
