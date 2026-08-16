"""add client identity tables: contact, writer_contact, client_import
(infra PRD §3.2, §5.1 — Phase 1 identity layer)

Revision ID: q2r3s4t5u6v7
Revises: p1q2r3s4t5u6
Create Date: 2026-07-06 00:00:00.000000

"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = "q2r3s4t5u6v7"
down_revision = "p1q2r3s4t5u6"
branch_labels = None
depends_on = None


writer_kind_enum = postgresql.ENUM(
    "CLIENT", "COMMISSION_PARTNER", name="writerkind", create_type=False
)
contact_role_enum = postgresql.ENUM(
    "PRIMARY", "MANAGER", "LEGAL", "OTHER", name="contactrole", create_type=False
)
client_import_status_enum = postgresql.ENUM(
    "PENDING_REVIEW", "APPLIED", "REJECTED", name="clientimportstatus", create_type=False
)

NEW_ENUMS = [writer_kind_enum, contact_role_enum, client_import_status_enum]


def upgrade() -> None:
    bind = op.get_bind()
    for enum_type in NEW_ENUMS:
        enum_type.create(bind, checkfirst=True)

    # Writer: client-list provenance columns
    op.add_column("writer", sa.Column("kind", writer_kind_enum, nullable=True))
    op.add_column("writer", sa.Column("payee_name", sa.String(), nullable=True))
    op.add_column("writer", sa.Column("preferred_language", sa.String(length=2), nullable=True))

    op.create_table(
        "contact",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("email", sa.String(), nullable=False),
        sa.Column("display_name", sa.String(), nullable=True),
        sa.Column("preferred_language", sa.String(length=2), nullable=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("User.id"), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("email", name="uq_contact_email"),
    )
    op.create_index("ix_contact_email", "contact", ["email"])
    op.create_index("ix_contact_user_id", "contact", ["user_id"])

    op.create_table(
        "writer_contact",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("writer_id", sa.Integer(), sa.ForeignKey("writer.id"), nullable=False),
        sa.Column("contact_id", sa.Integer(), sa.ForeignKey("contact.id"), nullable=False),
        sa.Column("role", contact_role_enum, nullable=False),
        sa.UniqueConstraint("writer_id", "contact_id", name="uq_writer_contact"),
    )
    op.create_index("ix_writer_contact_writer_id", "writer_contact", ["writer_id"])
    op.create_index("ix_writer_contact_contact_id", "writer_contact", ["contact_id"])

    op.create_table(
        "client_import",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("publisher_id", sa.Integer(), sa.ForeignKey("publisher.id"), nullable=True),
        sa.Column("filename", sa.String(), nullable=True),
        sa.Column("sha256", sa.String(length=64), nullable=True),
        sa.Column("uploaded_by", sa.Integer(), sa.ForeignKey("User.id"), nullable=True),
        sa.Column("uploaded_at", sa.DateTime(), nullable=False),
        sa.Column("status", client_import_status_enum, nullable=False),
        sa.Column("row_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("diff", sa.JSON(), nullable=True),
        sa.Column("findings", sa.JSON(), nullable=True),
        sa.Column("stats", sa.JSON(), nullable=True),
        sa.Column("applied_at", sa.DateTime(), nullable=True),
        sa.Column("applied_by", sa.Integer(), sa.ForeignKey("User.id"), nullable=True),
    )
    op.create_index("ix_client_import_publisher_id", "client_import", ["publisher_id"])
    op.create_index("ix_client_import_sha256", "client_import", ["sha256"])


def downgrade() -> None:
    op.drop_table("client_import")
    op.drop_table("writer_contact")
    op.drop_index("ix_contact_user_id", table_name="contact")
    op.drop_index("ix_contact_email", table_name="contact")
    op.drop_table("contact")
    op.drop_column("writer", "preferred_language")
    op.drop_column("writer", "payee_name")
    op.drop_column("writer", "kind")

    bind = op.get_bind()
    for enum_type in NEW_ENUMS:
        enum_type.drop(bind, checkfirst=True)
