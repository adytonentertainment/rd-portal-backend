"""auth_throttle: durable login throttling

Login lockout lived in a dict on the running process, so every deploy and every
container restart wiped it. On Render that happens routinely, which meant a
brute-force attempt did not need to outwait the 30-minute lockout — it only had
to outlast the next deploy.

Revision ID: b3c4d5e6f7a8
Revises: a2b3c4d5e6f7
"""

import sqlalchemy as sa
from alembic import op

revision = "b3c4d5e6f7a8"
down_revision = "a2b3c4d5e6f7"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "auth_throttle",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("scope", sa.String(length=32), nullable=False),
        sa.Column("identifier", sa.String(length=320), nullable=False),
        sa.Column("count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("window_start", sa.DateTime(), nullable=False),
        sa.Column("locked_until", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("scope", "identifier", name="uq_auth_throttle_scope_identifier"),
    )
    op.create_index("ix_auth_throttle_scope", "auth_throttle", ["scope"])
    # Lookups are always (scope, identifier); the unique constraint already
    # covers that, and this index serves the purge sweep by age.
    op.create_index("ix_auth_throttle_updated_at", "auth_throttle", ["updated_at"])


def downgrade():
    op.drop_index("ix_auth_throttle_updated_at", table_name="auth_throttle")
    op.drop_index("ix_auth_throttle_scope", table_name="auth_throttle")
    op.drop_table("auth_throttle")
