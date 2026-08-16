"""Publisher-side statement domain (PRD docs/PRD-statement-data-integration.md §5).

Parallel to the self-serve RevenueStatement/RevenueTransaction flow — do not
overload those. Phase 1 tables only: account_ledger / distribution come in
later phases.
"""

from datetime import datetime
from enum import Enum as PyEnum

from sqlalchemy import (
    JSON,
    Boolean,
    Column,
    Date,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import relationship

from ..database import Base


class Catalog(PyEnum):
    MECH = "MECH"
    YT = "YT"
    PERF = "PERF"


class Cadence(PyEnum):
    SEMIANNUAL = "semiannual"
    QUARTERLY = "quarterly"
    MIXED = "mixed"


class WriterStatus(PyEnum):
    ACTIVE = "active"
    OFFBOARDED = "offboarded"


class AliasSource(PyEnum):
    IMPORT = "import"
    MANUAL = "manual"


class AccountStatus(PyEnum):
    ACTIVE = "active"
    CLOSED = "closed"
    SUPERSEDED = "superseded"


class UploadStatus(PyEnum):
    UPLOADED = "uploaded"
    SORTING = "sorting"
    PARSING = "parsing"
    VALIDATING = "validating"
    DONE = "done"
    FAILED = "failed"


class BatchStatus(PyEnum):
    UPLOADED = "uploaded"
    PARSING = "parsing"
    PARSED = "parsed"
    VALIDATING = "validating"
    NEEDS_REVIEW = "needs_review"
    APPROVED = "approved"
    DISTRIBUTED = "distributed"
    ARCHIVED = "archived"


class ParseStatus(PyEnum):
    PENDING = "pending"
    PARSED = "parsed"
    FAILED = "failed"


class ZeroPayReason(PyEnum):
    PAID = "paid"
    THRESHOLD_CARRYOVER = "threshold_carryover"
    RECOUPED = "recouped"
    ZERO_EARNINGS = "zero_earnings"


class FindingSeverity(PyEnum):
    BLOCKER = "blocker"
    WARNING = "warning"
    INFO = "info"


class FindingScope(PyEnum):
    BATCH = "batch"
    STATEMENT = "statement"
    ACCOUNT = "account"
    WRITER = "writer"


class FindingStatus(PyEnum):
    OPEN = "open"
    RESOLVED = "resolved"
    WAIVED = "waived"


class WriterKind(PyEnum):
    """Which client-list sheet a writer came from (infra PRD §3.2)."""

    CLIENT = "client"
    COMMISSION_PARTNER = "commission_partner"


class ContactRole(PyEnum):
    """A contact's relationship to a writer. Managers/attorneys represent
    many writers, so this is never 1:1 (infra PRD §3.2, §7.2)."""

    PRIMARY = "primary"
    MANAGER = "manager"
    LEGAL = "legal"
    OTHER = "other"


class ClientImportStatus(PyEnum):
    PENDING_REVIEW = "pending_review"
    APPLIED = "applied"
    REJECTED = "rejected"


class MatchConfidence(PyEnum):
    EXACT = "exact"
    PROBABLE = "probable"
    NONE = "none"
    CONFIRMED = "confirmed"  # admin-confirmed, persists across imports


class Publisher(Base):
    __tablename__ = "publisher"

    id = Column(Integer, primary_key=True)
    name = Column(String, nullable=False, unique=True)
    default_fee_pct = Column(Numeric(5, 2), nullable=True)
    payout_threshold_usd = Column(Numeric(14, 6), nullable=True)

    writers = relationship("Writer", back_populates="publisher")


class Writer(Base):
    __tablename__ = "writer"

    id = Column(Integer, primary_key=True)
    publisher_id = Column(Integer, ForeignKey("publisher.id"), nullable=False, index=True)
    canonical_name = Column(String, nullable=False, index=True)
    status = Column(Enum(WriterStatus), nullable=False, default=WriterStatus.ACTIVE)

    # Reserved for the deferred email feature; client list pending
    contact_email = Column(String, nullable=True)
    portal_user_id = Column(Integer, ForeignKey("User.id"), nullable=True)

    # JSON list of Catalog values, e.g. ["MECH", "YT"]
    expected_catalogs = Column(JSON, nullable=True)
    cadence = Column(Enum(Cadence), nullable=True)
    is_house_account = Column(Boolean, nullable=False, default=False)
    # Roster membership, straight from the client list's two sheets. `kind` can
    # only hold ONE value, but the same person legitimately appears on both
    # sheets (a client for their own works, a commission partner on others'),
    # so these flags — not `kind` — are what the roster counts are built from.
    is_client = Column(Boolean, nullable=False, default=False)
    is_commission_partner = Column(Boolean, nullable=False, default=False)

    # Client-list provenance (infra PRD §3.2)
    kind = Column(Enum(WriterKind), nullable=True)
    payee_name = Column(String, nullable=True)  # legal payee; best match key
    preferred_language = Column(String(2), nullable=True)  # "en" | "es"

    publisher = relationship("Publisher", back_populates="writers")
    aliases = relationship("WriterAlias", back_populates="writer")
    accounts = relationship("BeneficiaryAccount", back_populates="writer")
    contact_links = relationship("WriterContact", back_populates="writer")


class Contact(Base):
    """A human who can log into the portal — writer, manager, or attorney.
    One email == one contact; a contact can be linked to many writers, and a
    writer to many contacts (infra PRD §3.2: 132 emails are shared)."""

    __tablename__ = "contact"

    id = Column(Integer, primary_key=True)
    email = Column(String, nullable=False, unique=True, index=True)
    display_name = Column(String, nullable=True)
    preferred_language = Column(String(2), nullable=True)
    # Set once the invite is accepted and a login exists.
    user_id = Column(Integer, ForeignKey("User.id"), nullable=True, index=True)

    created_at = Column(DateTime, default=datetime.now, nullable=False)

    writer_links = relationship("WriterContact", back_populates="contact")


class WriterContact(Base):
    """Many-to-many writer <-> contact with the contact's role."""

    __tablename__ = "writer_contact"
    __table_args__ = (
        UniqueConstraint("writer_id", "contact_id", name="uq_writer_contact"),
    )

    id = Column(Integer, primary_key=True)
    writer_id = Column(Integer, ForeignKey("writer.id"), nullable=False, index=True)
    contact_id = Column(Integer, ForeignKey("contact.id"), nullable=False, index=True)
    role = Column(Enum(ContactRole), nullable=False, default=ContactRole.PRIMARY)

    writer = relationship("Writer", back_populates="contact_links")
    contact = relationship("Contact", back_populates="writer_links")


class Distribution(Base):
    """A published statement in a writer's portal (ingestion PRD Stage C).

    Idempotent: at most one active (portal_visible, not superseded) distribution
    per statement. Reversible: unpublish flips portal_visible off but keeps the
    row. A re-ingested/corrected statement supersedes the prior one and links
    back via `superseded_by`. `period_code`/`catalog` are denormalized for the
    portal read path and cadence de-dup.
    """

    __tablename__ = "distribution"

    id = Column(Integer, primary_key=True)
    statement_id = Column(Integer, ForeignKey("statement.id"), nullable=False, index=True)
    writer_id = Column(Integer, ForeignKey("writer.id"), nullable=False, index=True)
    batch_id = Column(Integer, ForeignKey("statement_batch.id"), nullable=False, index=True)

    period_code = Column(String, nullable=False, index=True)
    catalog = Column(Enum(Catalog), nullable=False)

    published_at = Column(DateTime, default=datetime.now, nullable=False)
    published_by = Column(Integer, ForeignKey("User.id"), nullable=True)
    portal_visible = Column(Boolean, nullable=False, default=True, index=True)

    superseded_by = Column(Integer, ForeignKey("distribution.id"), nullable=True)
    # What the readiness gate looked like at publish time (audit).
    gate_snapshot = Column(JSON, nullable=True)

    statement = relationship("Statement")
    writer = relationship("Writer")


class PortalInvite(Base):
    """A pending share of one writer's portal access to an email address
    (infra PRD §7.2, refined 2026-07-06 to Dropbox-style sharing: anyone with
    access to a writer can invite another email from their settings; the admin
    sends the first invite to bootstrap each writer).

    Single-use, hashed token. 'Active' = not accepted, not revoked, not expired;
    creating a new invite for the same (writer, email) revokes the prior one.
    """

    __tablename__ = "portal_invite"

    id = Column(Integer, primary_key=True)
    writer_id = Column(Integer, ForeignKey("writer.id"), nullable=False, index=True)
    email = Column(String, nullable=False, index=True)
    token_hash = Column(String(64), nullable=False, unique=True, index=True)
    role = Column(Enum(ContactRole), nullable=False, default=ContactRole.MANAGER)

    # Who sent it: an admin or an existing contact's login. Null-safe either way.
    invited_by_user_id = Column(Integer, ForeignKey("User.id"), nullable=True)
    is_admin_invite = Column(Boolean, nullable=False, default=False)

    created_at = Column(DateTime, default=datetime.now, nullable=False)
    expires_at = Column(DateTime, nullable=False)
    accepted_at = Column(DateTime, nullable=True)
    revoked_at = Column(DateTime, nullable=True)

    # Delivery of the invite email. The invite is valid whether or not the mail
    # got through, so a send failure is recorded rather than raised — the admin
    # can still copy the link. 'pending' until the background send resolves.
    delivery_status = Column(String(16), nullable=False, default="pending")
    delivery_error = Column(String, nullable=True)
    sent_at = Column(DateTime, nullable=True)

    writer = relationship("Writer")

    def is_active(self, now: datetime) -> bool:
        return (
            self.accepted_at is None
            and self.revoked_at is None
            and self.expires_at > now
        )


class ClientImport(Base):
    """One upload of the client-list spreadsheet. Holds the computed diff and
    findings for admin review before apply — idempotent (infra PRD §3.2)."""

    __tablename__ = "client_import"

    id = Column(Integer, primary_key=True)
    publisher_id = Column(Integer, ForeignKey("publisher.id"), nullable=True, index=True)
    filename = Column(String, nullable=True)
    sha256 = Column(String(64), nullable=True, index=True)
    uploaded_by = Column(Integer, ForeignKey("User.id"), nullable=True)
    uploaded_at = Column(DateTime, default=datetime.now, nullable=False)

    status = Column(
        Enum(ClientImportStatus), nullable=False, default=ClientImportStatus.PENDING_REVIEW
    )
    row_count = Column(Integer, nullable=False, default=0)
    # Structured diff + findings + match summary, all computed at upload time.
    diff = Column(JSON, nullable=True)
    findings = Column(JSON, nullable=True)
    stats = Column(JSON, nullable=True)

    applied_at = Column(DateTime, nullable=True)
    applied_by = Column(Integer, ForeignKey("User.id"), nullable=True)


class WriterAlias(Base):
    __tablename__ = "writer_alias"

    id = Column(Integer, primary_key=True)
    writer_id = Column(Integer, ForeignKey("writer.id"), nullable=False, index=True)
    alias_name = Column(String, nullable=False, index=True)
    source = Column(Enum(AliasSource), nullable=False, default=AliasSource.IMPORT)
    created_at = Column(DateTime, default=datetime.now, nullable=False)

    writer = relationship("Writer", back_populates="aliases")


class BeneficiaryAccount(Base):
    __tablename__ = "beneficiary_account"

    id = Column(Integer, primary_key=True)
    writer_id = Column(Integer, ForeignKey("writer.id"), nullable=False, index=True)
    account_code = Column(String, nullable=False, unique=True, index=True)
    # The name printed on the statement FILENAME for this account — the
    # account's immutable identity. Ownership (writer_id) can be re-pointed by
    # imports/merges, but matching must always run against this original name,
    # never the current owner's (a bad merge would otherwise erase the identity
    # and become unrecoverable/self-reinforcing).
    display_name = Column(String, nullable=True)
    catalog = Column(Enum(Catalog), nullable=True)
    cadence = Column(Enum(Cadence), nullable=True)
    status = Column(Enum(AccountStatus), nullable=False, default=AccountStatus.ACTIVE)
    superseded_by = Column(Integer, ForeignKey("beneficiary_account.id"), nullable=True)
    opened_period = Column(String, nullable=True)
    closed_period = Column(String, nullable=True)

    # House accounts (CPJ001, CS0001) legitimately ship only one file kind
    pdf_only = Column(Boolean, nullable=False, default=False)
    xlsx_only = Column(Boolean, nullable=False, default=False)

    writer = relationship("Writer", back_populates="accounts")
    statements = relationship("Statement", back_populates="account")


class StatementUpload(Base):
    __tablename__ = "statement_upload"

    id = Column(Integer, primary_key=True)
    uploaded_by = Column(Integer, ForeignKey("User.id"), nullable=True)
    uploaded_at = Column(DateTime, default=datetime.now, nullable=False)
    file_count = Column(Integer, nullable=False, default=0)
    status = Column(Enum(UploadStatus), nullable=False, default=UploadStatus.UPLOADED)
    stats = Column(JSON, nullable=True)
    # Worker lease. An upload is only advanced by the worker holding the lease,
    # so two runners (a systemd worker plus the in-process one, or two API
    # replicas) can never parse the same upload concurrently and double-insert
    # its lines. The lease is heartbeated while a stage runs and expires on its
    # own, so a crashed worker's upload is picked back up instead of wedging.
    claimed_at = Column(DateTime, nullable=True)
    claimed_by = Column(String, nullable=True)

    batches = relationship("StatementBatch", back_populates="upload")


class StatementBatch(Base):
    __tablename__ = "statement_batch"

    id = Column(Integer, primary_key=True)
    publisher_id = Column(Integer, ForeignKey("publisher.id"), nullable=True, index=True)
    label = Column(String, nullable=False)  # derivable: "YouTube 2026H1"
    period_code = Column(String, nullable=False, index=True)  # PUB26H1
    catalog = Column(Enum(Catalog), nullable=False)
    cadence = Column(Enum(Cadence), nullable=True)

    # Batches are auto-derived from uploaded files, never hand-created
    upload_id = Column(Integer, ForeignKey("statement_upload.id"), nullable=True, index=True)
    uploaded_by = Column(Integer, ForeignKey("User.id"), nullable=True)
    uploaded_at = Column(DateTime, default=datetime.now, nullable=False)

    status = Column(Enum(BatchStatus), nullable=False, default=BatchStatus.UPLOADED)
    stats = Column(JSON, nullable=True)
    # Optional admin-entered expected payout for V-BATCH-9
    control_total = Column(Numeric(14, 6), nullable=True)

    upload = relationship("StatementUpload", back_populates="batches")
    statements = relationship("Statement", back_populates="batch")
    validation_runs = relationship("ValidationRun", back_populates="batch")


class Statement(Base):
    __tablename__ = "statement"
    __table_args__ = (
        UniqueConstraint(
            "account_id", "period_code", "version", name="uq_statement_account_period_version"
        ),
    )

    id = Column(Integer, primary_key=True)
    batch_id = Column(Integer, ForeignKey("statement_batch.id"), nullable=False, index=True)
    account_id = Column(Integer, ForeignKey("beneficiary_account.id"), nullable=False, index=True)
    period_code = Column(String, nullable=False)
    version = Column(Integer, nullable=False, default=1)

    pdf_path = Column(String(500), nullable=True)
    xlsx_path = Column(String(500), nullable=True)
    statement_date = Column(Date, nullable=True)

    # Parsed from PDF account summary (None == absent in the old layout, never 0.0)
    calculated = Column(Numeric(14, 6), nullable=True)
    recouped = Column(Numeric(14, 6), nullable=True)
    reserve_taken = Column(Numeric(14, 6), nullable=True)
    reserve_released = Column(Numeric(14, 6), nullable=True)
    carried_forward_in = Column(Numeric(14, 6), nullable=True)
    payable_prev = Column(Numeric(14, 6), nullable=True)
    settlement_paid = Column(Numeric(14, 6), nullable=True)
    payable = Column(Numeric(14, 6), nullable=True)
    before_tax = Column(Numeric(14, 6), nullable=True)
    payable_this = Column(Numeric(14, 6), nullable=True)
    # 'Carried forward to Next period' — below-threshold money leaving the
    # account; distinct from carried_forward_in (V-STMT-3 must subtract it)
    carried_forward_out = Column(Numeric(14, 6), nullable=True)
    # Page-1 cheque line ('cheque of USD X'), when present
    cheque_amount = Column(Numeric(14, 6), nullable=True)

    # Computed from XLSX detail
    detail_sum = Column(Numeric(14, 6), nullable=True)
    embedded_total = Column(Numeric(14, 6), nullable=True)
    line_count = Column(Integer, nullable=True)

    zero_pay_reason = Column(Enum(ZeroPayReason), nullable=True)
    parse_status = Column(Enum(ParseStatus), nullable=False, default=ParseStatus.PENDING)
    parse_error = Column(String, nullable=True)

    batch = relationship("StatementBatch", back_populates="statements")
    account = relationship("BeneficiaryAccount", back_populates="statements")
    lines = relationship("StatementLine", back_populates="statement")


class StatementLine(Base):
    __tablename__ = "statement_line"
    __table_args__ = (Index("ix_statement_line_statement_id", "statement_id"),)

    id = Column(Integer, primary_key=True)
    statement_id = Column(Integer, ForeignKey("statement.id"), nullable=False)
    row_no = Column(Integer, nullable=False)

    # Superset of both XLSX schemas (PRD §2.4); nullable where catalog-specific
    song_code = Column(String, nullable=True)
    asset_id = Column(String, nullable=True)
    custom_id = Column(String, nullable=True)
    song_title = Column(String, nullable=True)
    country = Column(String, nullable=True)
    channel = Column(String, nullable=True)
    income_source = Column(String, nullable=True)
    income_type = Column(String, nullable=True)
    price = Column(Numeric(14, 6), nullable=True)
    commission_pct = Column(Numeric(9, 6), nullable=True)
    rbp = Column(Numeric(14, 6), nullable=True)
    rate_applied = Column(Numeric(14, 6), nullable=True)
    writer_split_pct = Column(Numeric(9, 6), nullable=True)
    ben_split_pct = Column(Numeric(9, 6), nullable=True)
    units = Column(Numeric(14, 4), nullable=True)
    earnings = Column(Numeric(14, 6), nullable=True)

    statement = relationship("Statement", back_populates="lines")


class ValidationRun(Base):
    __tablename__ = "validation_run"

    id = Column(Integer, primary_key=True)
    batch_id = Column(Integer, ForeignKey("statement_batch.id"), nullable=False, index=True)
    started_at = Column(DateTime, default=datetime.now, nullable=False)
    finished_at = Column(DateTime, nullable=True)
    rules_version = Column(String, nullable=True)
    blockers = Column(Integer, nullable=False, default=0)
    warnings = Column(Integer, nullable=False, default=0)
    infos = Column(Integer, nullable=False, default=0)

    batch = relationship("StatementBatch", back_populates="validation_runs")
    findings = relationship("ValidationFinding", back_populates="run")


class ValidationFinding(Base):
    __tablename__ = "validation_finding"

    id = Column(Integer, primary_key=True)
    run_id = Column(Integer, ForeignKey("validation_run.id"), nullable=False, index=True)
    rule_id = Column(String, nullable=False, index=True)  # e.g. V-FILE-1
    severity = Column(Enum(FindingSeverity), nullable=False)
    scope = Column(Enum(FindingScope), nullable=False)
    scope_ref = Column(String, nullable=True)
    message = Column(String, nullable=False)
    details = Column(JSON, nullable=True)

    status = Column(Enum(FindingStatus), nullable=False, default=FindingStatus.OPEN)
    waived_by = Column(Integer, ForeignKey("User.id"), nullable=True)
    waived_reason = Column(String, nullable=True)
    waived_at = Column(DateTime, nullable=True)
    # Acknowledgement is logged metadata, NOT a status (PRD §5 keeps status
    # open|resolved|waived; §7.3: warnings must be acknowledged, logged) —
    # the finding stays open, the Phase-3 gate checks acknowledged_at
    acknowledged_by = Column(Integer, ForeignKey("User.id"), nullable=True)
    acknowledged_at = Column(DateTime, nullable=True)

    run = relationship("ValidationRun", back_populates="findings")
