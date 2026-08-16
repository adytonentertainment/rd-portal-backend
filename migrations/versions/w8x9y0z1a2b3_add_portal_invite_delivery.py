"""Record whether an invite email actually went out.

An invite is valid whether or not the mail was delivered — the token works
either way and the admin can copy the link. So delivery is recorded on the
invite instead of failing its creation, which is also the only way to answer
"did they ever get it?" when a writer says they never saw an invite.

Revision ID: w8x9y0z1a2b3
Revises: v7w8x9y0z1a2
"""

import sqlalchemy as sa
from alembic import op

revision = "w8x9y0z1a2b3"
down_revision = "v7w8x9y0z1a2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Existing invites predate email delivery: they were shared as copied links,
    # so 'not_sent' is the truthful backfill — not 'pending', which would imply
    # a send is still coming.
    op.add_column(
        "portal_invite",
        sa.Column("delivery_status", sa.String(16), nullable=False, server_default="not_sent"),
    )
    op.add_column("portal_invite", sa.Column("delivery_error", sa.String(), nullable=True))
    op.add_column("portal_invite", sa.Column("sent_at", sa.DateTime(), nullable=True))


def downgrade() -> None:
    op.drop_column("portal_invite", "sent_at")
    op.drop_column("portal_invite", "delivery_error")
    op.drop_column("portal_invite", "delivery_status")
