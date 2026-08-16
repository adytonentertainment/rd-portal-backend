"""add statement_upload.claimed_at / claimed_by (ingest worker lease)

The ingest pipeline is advanced by a polling worker. Without a lease, two
runners — a dedicated worker process and the in-process one, or two API
replicas — can pick up the same upload and run its parse stage concurrently,
inserting every StatementLine twice and doubling the earnings a writer sees.
The lease makes claiming atomic (conditional UPDATE) and expires on its own so
a crashed worker's upload is retried rather than stuck forever.

Revision ID: v7w8x9y0z1a2
Revises: u6v7w8x9y0z1
Create Date: 2026-08-11 00:00:00.000000

"""

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "v7w8x9y0z1a2"
down_revision = "u6v7w8x9y0z1"
branch_labels = None
depends_on = None


def _existing_columns(table: str) -> set:
    inspector = sa.inspect(op.get_bind())
    if table not in inspector.get_table_names():
        return set()
    return {c["name"] for c in inspector.get_columns(table)}


def upgrade() -> None:
    columns = _existing_columns("statement_upload")
    if "claimed_at" not in columns:
        op.add_column(
            "statement_upload", sa.Column("claimed_at", sa.DateTime(), nullable=True)
        )
    if "claimed_by" not in columns:
        op.add_column(
            "statement_upload", sa.Column("claimed_by", sa.String(), nullable=True)
        )


def downgrade() -> None:
    columns = _existing_columns("statement_upload")
    for name in ("claimed_by", "claimed_at"):
        if name in columns:
            op.drop_column("statement_upload", name)
