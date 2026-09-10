"""Mark acquired catalogs as publisher-owned.

Some roster entries are catalogs the writer SOLD to the publisher. They stay on
the roster under that writer's name so the publisher can account for them, but
the money is no longer the writer's and they must not be able to read it —
"Likybo" (old, acquired) versus "Likybo NEW" (their current catalog).

Until now that distinction lived only inside the entry's NAME, as a convention:
`NEW`, `(Regalias)`, `(100% to Regalias)`. Nothing enforced it. A writer invited
to the wrong one of those two entries would have been shown a catalog they no
longer own, and the only thing standing in the way was somebody reading the
name carefully.

`is_house_account` was the closest existing flag, but it means the publisher's
OWN books (CPJ001, CS0001) and lumps them together in reporting. These are
reported per writer, so they get their own attribute.

Defaults false: every existing row keeps its current behaviour, and the entries
that are acquired have to be marked deliberately.

Revision ID: z1a2b3c4d5e6
Revises: y0z1a2b3c4d5
"""

import sqlalchemy as sa
from alembic import op

revision = "z1a2b3c4d5e6"
down_revision = "y0z1a2b3c4d5"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "writer",
        sa.Column(
            "publisher_owned",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )


def downgrade() -> None:
    op.drop_column("writer", "publisher_owned")
