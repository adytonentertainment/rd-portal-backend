"""Portal identity is per CLIENT, not per mailbox.

A manager or attorney represents several writers, so the same address
legitimately claims several portals. Keyed on email, those collapsed into one
login: accepting a second client attached it to the first account, and signing
in showed both catalogs together. One person's royalties were one click from
another's, and there was no way to hand someone access to exactly one client.

So a claim now belongs to the (writer, contact) link rather than to the address:

  * `writer_contact.user_id` — the login that claimed THIS client. Null until
    an invite for it is accepted.
  * `User.email` loses its UNIQUE constraint, because one mailbox now backs
    several logins. `username` stays unique and becomes the identity someone
    actually signs in with; accept_invite defaults it to the client's name.

Backfill: existing claims live on `contact.user_id`, which was per-address.
Each of those is copied onto every link that contact holds — the honest
reading of the old data, since that login really could see all of them. Nothing
is revoked here; the tightened access rule lives in the application layer.

Revision ID: y0z1a2b3c4d5
Revises: x9y0z1a2b3c4
"""

import sqlalchemy as sa
from alembic import op

revision = "y0z1a2b3c4d5"
down_revision = "x9y0z1a2b3c4"
branch_labels = None
depends_on = None


def _user_email_unique_constraints(bind) -> list:
    """Whatever the unique index/constraint on User.email is called here.

    It was created implicitly by `unique=True`, so the name differs between
    SQLite (an auto index) and Postgres (`User_email_key`), and dropping it
    blind fails on one of them.
    """
    insp = sa.inspect(bind)
    names = []
    for uc in insp.get_unique_constraints("User"):
        if uc.get("column_names") == ["email"]:
            names.append(("constraint", uc["name"]))
    for ix in insp.get_indexes("User"):
        if ix.get("unique") and ix.get("column_names") == ["email"]:
            names.append(("index", ix["name"]))
    return names


def upgrade() -> None:
    bind = op.get_bind()

    op.add_column("writer_contact", sa.Column("user_id", sa.Integer(), nullable=True))
    op.create_index(
        "ix_writer_contact_user_id", "writer_contact", ["user_id"], unique=False
    )

    # Carry existing claims from the address onto each client it could reach.
    op.execute(
        """
        UPDATE writer_contact
           SET user_id = (
               SELECT c.user_id FROM contact c
                WHERE c.id = writer_contact.contact_id
           )
         WHERE EXISTS (
               SELECT 1 FROM contact c
                WHERE c.id = writer_contact.contact_id
                  AND c.user_id IS NOT NULL
         )
        """
    )

    # One mailbox, several logins.
    for kind, name in _user_email_unique_constraints(bind):
        if bind.dialect.name == "sqlite":
            # SQLite cannot drop a constraint in place; rebuild the table.
            with op.batch_alter_table("User") as batch:
                if kind == "index":
                    batch.drop_index(name)
                else:
                    batch.drop_constraint(name, type_="unique")
        elif kind == "index":
            op.drop_index(name, table_name="User")
        else:
            op.drop_constraint(name, "User", type_="unique")

    # Keep the lookup fast — it is still the address book key, just not unique.
    insp = sa.inspect(bind)
    if not any(ix["column_names"] == ["email"] for ix in insp.get_indexes("User")):
        op.create_index("ix_User_email", "User", ["email"], unique=False)


def downgrade() -> None:
    # Re-imposing uniqueness would fail wherever a mailbox has claimed more
    # than one client, which is the whole point of the change; restoring it is
    # a data decision, not a mechanical one.
    op.drop_index("ix_writer_contact_user_id", table_name="writer_contact")
    op.drop_column("writer_contact", "user_id")
