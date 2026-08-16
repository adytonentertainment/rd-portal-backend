"""add statement domain tables (PRD statement-data-integration §5, Phase 1)

Revision ID: n9o0p1q2r3s4
Revises: m8n9o0p1q2r3
Create Date: 2026-06-11 00:00:00.000000

"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = "n9o0p1q2r3s4"
down_revision = "m8n9o0p1q2r3"
branch_labels = None
depends_on = None


def _enum(name, *values):
    """Shared enum type: created once in upgrade(), referenced by columns."""
    return postgresql.ENUM(*values, name=name, create_type=False)


catalog_enum = _enum("catalog", "MECH", "YT", "PERF")
cadence_enum = _enum("cadence", "SEMIANNUAL", "QUARTERLY", "MIXED")
writer_status_enum = _enum("writerstatus", "ACTIVE", "OFFBOARDED")
alias_source_enum = _enum("aliassource", "IMPORT", "MANUAL")
account_status_enum = _enum("accountstatus", "ACTIVE", "CLOSED", "SUPERSEDED")
upload_status_enum = _enum(
    "uploadstatus", "UPLOADED", "SORTING", "PARSING", "VALIDATING", "DONE", "FAILED"
)
batch_status_enum = _enum(
    "batchstatus",
    "UPLOADED",
    "PARSING",
    "PARSED",
    "VALIDATING",
    "NEEDS_REVIEW",
    "APPROVED",
    "DISTRIBUTED",
    "ARCHIVED",
)
parse_status_enum = _enum("parsestatus", "PENDING", "PARSED", "FAILED")
zero_pay_reason_enum = _enum(
    "zeropayreason", "PAID", "THRESHOLD_CARRYOVER", "RECOUPED", "ZERO_EARNINGS"
)
finding_severity_enum = _enum("findingseverity", "BLOCKER", "WARNING", "INFO")
finding_scope_enum = _enum("findingscope", "BATCH", "STATEMENT", "ACCOUNT", "WRITER")
finding_status_enum = _enum("findingstatus", "OPEN", "RESOLVED", "WAIVED")

ALL_ENUMS = [
    catalog_enum,
    cadence_enum,
    writer_status_enum,
    alias_source_enum,
    account_status_enum,
    upload_status_enum,
    batch_status_enum,
    parse_status_enum,
    zero_pay_reason_enum,
    finding_severity_enum,
    finding_scope_enum,
    finding_status_enum,
]


def upgrade() -> None:
    bind = op.get_bind()
    for enum_type in ALL_ENUMS:
        enum_type.create(bind, checkfirst=True)

    op.create_table(
        "publisher",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(), nullable=False, unique=True),
        sa.Column("default_fee_pct", sa.Numeric(5, 2), nullable=True),
        sa.Column("payout_threshold_usd", sa.Numeric(14, 6), nullable=True),
    )

    op.create_table(
        "writer",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "publisher_id", sa.Integer(), sa.ForeignKey("publisher.id"), nullable=False, index=True
        ),
        sa.Column("canonical_name", sa.String(), nullable=False, index=True),
        sa.Column("status", writer_status_enum, nullable=False),
        sa.Column("contact_email", sa.String(), nullable=True),
        sa.Column("portal_user_id", sa.Integer(), sa.ForeignKey("User.id"), nullable=True),
        sa.Column("expected_catalogs", sa.JSON(), nullable=True),
        sa.Column("cadence", cadence_enum, nullable=True),
        sa.Column("is_house_account", sa.Boolean(), nullable=False),
    )

    op.create_table(
        "writer_alias",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "writer_id", sa.Integer(), sa.ForeignKey("writer.id"), nullable=False, index=True
        ),
        sa.Column("alias_name", sa.String(), nullable=False, index=True),
        sa.Column("source", alias_source_enum, nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )

    op.create_table(
        "beneficiary_account",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "writer_id", sa.Integer(), sa.ForeignKey("writer.id"), nullable=False, index=True
        ),
        sa.Column("account_code", sa.String(), nullable=False, unique=True, index=True),
        sa.Column("catalog", catalog_enum, nullable=True),
        sa.Column("cadence", cadence_enum, nullable=True),
        sa.Column("status", account_status_enum, nullable=False),
        sa.Column(
            "superseded_by", sa.Integer(), sa.ForeignKey("beneficiary_account.id"), nullable=True
        ),
        sa.Column("opened_period", sa.String(), nullable=True),
        sa.Column("closed_period", sa.String(), nullable=True),
        sa.Column("pdf_only", sa.Boolean(), nullable=False),
        sa.Column("xlsx_only", sa.Boolean(), nullable=False),
    )

    op.create_table(
        "statement_upload",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("uploaded_by", sa.Integer(), sa.ForeignKey("User.id"), nullable=True),
        sa.Column("uploaded_at", sa.DateTime(), nullable=False),
        sa.Column("file_count", sa.Integer(), nullable=False),
        sa.Column("status", upload_status_enum, nullable=False),
        sa.Column("stats", sa.JSON(), nullable=True),
    )

    op.create_table(
        "statement_batch",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "publisher_id", sa.Integer(), sa.ForeignKey("publisher.id"), nullable=True, index=True
        ),
        sa.Column("label", sa.String(), nullable=False),
        sa.Column("period_code", sa.String(), nullable=False, index=True),
        sa.Column("catalog", catalog_enum, nullable=False),
        sa.Column("cadence", cadence_enum, nullable=True),
        sa.Column(
            "upload_id",
            sa.Integer(),
            sa.ForeignKey("statement_upload.id"),
            nullable=True,
            index=True,
        ),
        sa.Column("uploaded_by", sa.Integer(), sa.ForeignKey("User.id"), nullable=True),
        sa.Column("uploaded_at", sa.DateTime(), nullable=False),
        sa.Column("status", batch_status_enum, nullable=False),
        sa.Column("stats", sa.JSON(), nullable=True),
        sa.Column("control_total", sa.Numeric(14, 6), nullable=True),
    )

    op.create_table(
        "statement",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "batch_id",
            sa.Integer(),
            sa.ForeignKey("statement_batch.id"),
            nullable=False,
            index=True,
        ),
        sa.Column(
            "account_id",
            sa.Integer(),
            sa.ForeignKey("beneficiary_account.id"),
            nullable=False,
            index=True,
        ),
        sa.Column("period_code", sa.String(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("pdf_path", sa.String(500), nullable=True),
        sa.Column("xlsx_path", sa.String(500), nullable=True),
        sa.Column("statement_date", sa.Date(), nullable=True),
        sa.Column("calculated", sa.Numeric(14, 6), nullable=True),
        sa.Column("recouped", sa.Numeric(14, 6), nullable=True),
        sa.Column("reserve_taken", sa.Numeric(14, 6), nullable=True),
        sa.Column("reserve_released", sa.Numeric(14, 6), nullable=True),
        sa.Column("carried_forward_in", sa.Numeric(14, 6), nullable=True),
        sa.Column("payable_prev", sa.Numeric(14, 6), nullable=True),
        sa.Column("settlement_paid", sa.Numeric(14, 6), nullable=True),
        sa.Column("payable", sa.Numeric(14, 6), nullable=True),
        sa.Column("detail_sum", sa.Numeric(14, 6), nullable=True),
        sa.Column("embedded_total", sa.Numeric(14, 6), nullable=True),
        sa.Column("line_count", sa.Integer(), nullable=True),
        sa.Column("zero_pay_reason", zero_pay_reason_enum, nullable=True),
        sa.Column("parse_status", parse_status_enum, nullable=False),
        sa.Column("parse_error", sa.String(), nullable=True),
        sa.UniqueConstraint(
            "account_id", "period_code", "version", name="uq_statement_account_period_version"
        ),
    )

    op.create_table(
        "statement_line",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("statement_id", sa.Integer(), sa.ForeignKey("statement.id"), nullable=False),
        sa.Column("row_no", sa.Integer(), nullable=False),
        sa.Column("song_code", sa.String(), nullable=True),
        sa.Column("asset_id", sa.String(), nullable=True),
        sa.Column("custom_id", sa.String(), nullable=True),
        sa.Column("song_title", sa.String(), nullable=True),
        sa.Column("country", sa.String(), nullable=True),
        sa.Column("channel", sa.String(), nullable=True),
        sa.Column("income_source", sa.String(), nullable=True),
        sa.Column("income_type", sa.String(), nullable=True),
        sa.Column("price", sa.Numeric(14, 6), nullable=True),
        sa.Column("commission_pct", sa.Numeric(9, 6), nullable=True),
        sa.Column("rbp", sa.Numeric(14, 6), nullable=True),
        sa.Column("rate_applied", sa.Numeric(14, 6), nullable=True),
        sa.Column("writer_split_pct", sa.Numeric(9, 6), nullable=True),
        sa.Column("ben_split_pct", sa.Numeric(9, 6), nullable=True),
        sa.Column("units", sa.Numeric(14, 4), nullable=True),
        sa.Column("earnings", sa.Numeric(14, 6), nullable=True),
    )
    op.create_index("ix_statement_line_statement_id", "statement_line", ["statement_id"])

    op.create_table(
        "validation_run",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "batch_id",
            sa.Integer(),
            sa.ForeignKey("statement_batch.id"),
            nullable=False,
            index=True,
        ),
        sa.Column("started_at", sa.DateTime(), nullable=False),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.Column("rules_version", sa.String(), nullable=True),
        sa.Column("blockers", sa.Integer(), nullable=False),
        sa.Column("warnings", sa.Integer(), nullable=False),
        sa.Column("infos", sa.Integer(), nullable=False),
    )

    op.create_table(
        "validation_finding",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "run_id", sa.Integer(), sa.ForeignKey("validation_run.id"), nullable=False, index=True
        ),
        sa.Column("rule_id", sa.String(), nullable=False, index=True),
        sa.Column("severity", finding_severity_enum, nullable=False),
        sa.Column("scope", finding_scope_enum, nullable=False),
        sa.Column("scope_ref", sa.String(), nullable=True),
        sa.Column("message", sa.String(), nullable=False),
        sa.Column("details", sa.JSON(), nullable=True),
        sa.Column("status", finding_status_enum, nullable=False),
        sa.Column("waived_by", sa.Integer(), sa.ForeignKey("User.id"), nullable=True),
        sa.Column("waived_reason", sa.String(), nullable=True),
        sa.Column("waived_at", sa.DateTime(), nullable=True),
    )


def downgrade() -> None:
    op.drop_table("validation_finding")
    op.drop_table("validation_run")
    op.drop_index("ix_statement_line_statement_id", table_name="statement_line")
    op.drop_table("statement_line")
    op.drop_table("statement")
    op.drop_table("statement_batch")
    op.drop_table("statement_upload")
    op.drop_table("beneficiary_account")
    op.drop_table("writer_alias")
    op.drop_table("writer")
    op.drop_table("publisher")

    bind = op.get_bind()
    for enum_type in ALL_ENUMS:
        enum_type.drop(bind, checkfirst=True)
