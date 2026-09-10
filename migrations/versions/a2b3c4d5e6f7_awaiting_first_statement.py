"""Mark a newly signed client as awaiting their first statement.

A client signed after the last statement run has no statements and is not
supposed to. The roster reported them the same way it reports a client whose
statements failed to arrive: red, "no statements", counted as a reason not to
send. That is noise on a screen whose whole job is telling an admin what still
needs doing, and it buries the clients who really are missing something.

Defaults false so nothing changes for existing rows: an admin marks the ones
that are genuinely new.

Revision ID: a2b3c4d5e6f7
Revises: z1a2b3c4d5e6
"""

import sqlalchemy as sa
from alembic import op

revision = "a2b3c4d5e6f7"
down_revision = "z1a2b3c4d5e6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "writer",
        sa.Column(
            "awaiting_first_statement",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )


def downgrade() -> None:
    op.drop_column("writer", "awaiting_first_statement")
