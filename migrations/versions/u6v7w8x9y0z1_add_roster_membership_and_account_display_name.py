"""add writer.is_client / is_commission_partner + beneficiary_account.display_name

Roster membership (§ client list) and account identity (§ statement filenames).

`writer.kind` can only hold ONE value, but the delivered client list has two
sheets and the same person legitimately appears on BOTH (a client for their own
works, a commission partner on others'). The roster counts must mirror the
spreadsheet's own row counts, so membership is carried on two independent flags
rather than inferred from `kind`.

`beneficiary_account.display_name` stores the name printed on the account's
statement FILENAME — the account's immutable identity. Ownership (writer_id)
gets re-pointed by imports and merges; matching must always run against this
original name, otherwise a bad merge erases the identity and becomes
self-reinforcing (the rightful client-list row can never claim the account back).

Backfill is best-effort from data already in the DB:
  * is_client / is_commission_partner from the existing `kind`
  * display_name from the account's current owner name
Both are re-derived exactly on the next client-list import / statement ingest,
which are the authoritative sources.

Revision ID: u6v7w8x9y0z1
Revises: t5u6v7w8x9y0
Create Date: 2026-08-11 00:00:00.000000

"""

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "u6v7w8x9y0z1"
down_revision = "t5u6v7w8x9y0"
branch_labels = None
depends_on = None


# Minimal table stubs for the backfill. Declared locally (never imported from
# app.models) so this revision keeps working when the models move on.
_writer = sa.table(
    "writer",
    sa.column("id", sa.Integer),
    sa.column("canonical_name", sa.String),
    sa.column("kind", sa.String),
    sa.column("is_client", sa.Boolean),
    sa.column("is_commission_partner", sa.Boolean),
)
_account = sa.table(
    "beneficiary_account",
    sa.column("writer_id", sa.Integer),
    sa.column("display_name", sa.String),
)


def _existing_columns(table: str) -> set:
    """Columns already present — dev databases got these via raw ALTER before
    this revision existed, so every step below is skip-if-present."""
    inspector = sa.inspect(op.get_bind())
    if table not in inspector.get_table_names():
        return set()
    return {c["name"] for c in inspector.get_columns(table)}


def upgrade() -> None:
    writer_columns = _existing_columns("writer")
    account_columns = _existing_columns("beneficiary_account")

    # --- new columns -------------------------------------------------------
    # server_default keeps the NOT NULL add valid on existing rows; it is
    # dropped below so the schema matches the model (which defaults in Python).
    added_membership = []
    for name in ("is_client", "is_commission_partner"):
        if name not in writer_columns:
            op.add_column(
                "writer",
                sa.Column(name, sa.Boolean(), nullable=False, server_default=sa.false()),
            )
            added_membership.append(name)

    if "display_name" not in account_columns:
        op.add_column(
            "beneficiary_account", sa.Column("display_name", sa.String(), nullable=True)
        )

    # --- backfill ----------------------------------------------------------
    # `kind` is a Postgres ENUM (writerkind) and plain text on SQLite; cast so
    # the comparison is valid on both.
    kind_as_text = sa.cast(_writer.c.kind, sa.String)
    if added_membership:
        op.execute(
            _writer.update().where(kind_as_text == "CLIENT").values(is_client=True)
        )
        op.execute(
            _writer.update()
            .where(kind_as_text == "COMMISSION_PARTNER")
            .values(is_commission_partner=True)
        )

    if "display_name" not in account_columns:
        op.execute(
            _account.update()
            .where(_account.c.display_name.is_(None))
            .values(
                display_name=sa.select(_writer.c.canonical_name)
                .where(_writer.c.id == _account.c.writer_id)
                .scalar_subquery()
            )
        )

    # --- drop the transitional server_default ------------------------------
    # batch_alter_table so SQLite (which cannot ALTER a default) rebuilds the
    # table; on Postgres this is a plain ALTER COLUMN DROP DEFAULT.
    if added_membership:
        with op.batch_alter_table("writer") as batch:
            for name in added_membership:
                batch.alter_column(name, server_default=None)


def downgrade() -> None:
    account_columns = _existing_columns("beneficiary_account")
    writer_columns = _existing_columns("writer")

    if "display_name" in account_columns:
        op.drop_column("beneficiary_account", "display_name")
    for name in ("is_commission_partner", "is_client"):
        if name in writer_columns:
            op.drop_column("writer", name)
