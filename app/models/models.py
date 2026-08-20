from datetime import datetime
from enum import Enum as PyEnum

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    Column,
    Date,
    DateTime,
    Double,
    Enum,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import relationship

from ..database import Base


# will be created when an user creates an account.
class User(Base):
    __tablename__ = "User"

    id = Column(Integer, primary_key=True)
    # NOT unique: one mailbox can hold several portal logins, one per client.
    # A manager who represents three writers claims three portals from the same
    # inbox, each with its own username and password, so that opening one never
    # exposes the others. `username` is the unique identity — see
    # portal.accept_invite.
    email = Column(String, index=True, nullable=False)
    username = Column(String, nullable=False, unique=True)

    # users who log in with a service will not have a password
    hashed_password = Column(String, nullable=True)
    activated = Column(Boolean, default=False)

    # Verax account role. 'writer' is the default (portal user); 'admin' is the
    # publisher ops role. An admin account is only EFFECTIVE when admin_approved
    # is True — self-registered admins land pending until an existing admin
    # approves them (see app/routers/auth.py register + admin approval).
    role = Column(String, nullable=False, default="writer", server_default="writer")
    admin_approved = Column(Boolean, nullable=False, default=False, server_default="false")

    acrcloud_container_id = Column(Integer, nullable=True, unique=True)
    stripe_customer_id = Column(String, nullable=True, unique=True)
    spotify_user_id = Column(String, nullable=True, unique=True)
    genius_user_id = Column(String, nullable=True, unique=True)

    payment_method = Column(String, nullable=True, unique=True)

    subscription = relationship("Subscription", backref="user")
    user_catalog = relationship(
        "UserCatalog", back_populates="user", cascade="all, delete-orphan"
    )
    scans = relationship("ACRCloudScan", backref="user")

    payment_fingerprint = Column(String, nullable=True, unique=False)

    # where the user is located, used to estimate revenue
    royalty_per_stream = Column(Integer, nullable=False, unique=False)

    # Profile information
    first_name = Column(String, nullable=True)
    last_name = Column(String, nullable=True)
    ipi_number = Column(
        String, nullable=True
    )  # Legacy field, kept for backwards compatibility
    writer_ipi = Column(String, nullable=True)  # Writer IPI number (BMI, ASCAP, etc.)
    writer_name = Column(String, nullable=True)  # Writer/songwriter name for PRO registration
    publisher_ipi = Column(String, nullable=True)  # Publisher IPI number
    publisher_name = Column(String, nullable=True)  # Publisher/company name
    avatar_url = Column(String, nullable=True)

    # Notification settings stored as JSON: {type: {channel: enabled}}
    # Example: {"revenue_discrepancy": {"in_app": true, "email": true, "push": false}}
    notification_settings = Column(JSON, nullable=True)

    # Feature flags
    auto_register_enabled = Column(Boolean, default=False, nullable=False, server_default="false")

    @property
    def account_activated(self):
        """Alias for 'activated' column for backwards compatibility."""
        return self.activated

    @account_activated.setter
    def account_activated(self, value):
        """Alias setter for 'activated' column."""
        self.activated = value

    def get_active_subscription(self, stripe_mode: str = None):
        """
        Get the active subscription for the current stripe mode.

        Args:
            stripe_mode: 'test' or 'live'. If None, uses the global stripe_mode setting.

        Returns:
            The active Subscription object for the current mode, or None if no subscription exists.
        """
        if stripe_mode is None:
            from app.settings.settings import get_settings

            settings = get_settings()
            stripe_mode = settings.stripe_mode

        # Normalize mode to lowercase
        stripe_mode = stripe_mode.lower()

        # Filter subscriptions by mode and status
        for sub in self.subscription:
            if sub.stripe_mode and sub.stripe_mode.lower() == stripe_mode:
                if sub.stripe_status in ("active", "trialing"):
                    return sub

        return None


# part of the Subscription model. These are our tiers.
class SubscriptionTier(PyEnum):
    FREE = "Free"
    ESSENTIAL = "Essential"
    PRO = "Pro"
    ELITE = "Elite"
    ENTERPRISE = "Enterprise"

    def __str__(self):
        return str(self.value)


# if an user subscribes, a new entry in the subscription
# table is created.
class Subscription(Base):
    __tablename__ = "Subscription"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("User.id"))
    subscription_id = Column(String, unique=True)
    tier = Column(Enum(SubscriptionTier), nullable=False)
    scans = Column(Integer, nullable=False)

    # Track how many times catalog items have been added in the current billing period
    catalog_added_count = Column(Integer, nullable=False, default=0)

    # if true, the user has confirmed that they want to exceed the limit of their scans
    limit_exceeded = Column(Boolean, nullable=False)

    # Billing interval: "month" or "year"
    billing_interval = Column(String, nullable=False, default="month")

    # Track whether this subscription is in test or live mode
    stripe_mode = Column(String, nullable=False, default="test")

    # Stripe subscription status (incomplete, active, past_due, canceled, etc.)
    stripe_status = Column(String, nullable=False, default="incomplete")

    # Optional: Track when test subscriptions should transition to live
    mode_transition_date = Column(DateTime, nullable=True)


# contains all scanned files by the user.
class ACRCloudScan(Base):
    __tablename__ = "ACRCloudScan"

    id = Column(Integer, primary_key=True)
    acrcloud_file_id = Column(String, nullable=False)
    user_id = Column(Integer, ForeignKey("User.id"))
    client_id = Column(
        Integer, ForeignKey("Client.id", ondelete="SET NULL"), nullable=True, index=True
    )
    comprehensive_detection = Column(
        JSON, nullable=True
    )  # Store comprehensive detection results
    stored_file_path = Column(
        String, nullable=True
    )  # Path to stored audio file for rescans
    is_pending_auto_rescan = Column(
        Boolean, default=False, server_default="false"
    )  # Flag for pending auto-rescan notification

    # Relationships
    client = relationship("Client", backref="scans")


# Batch Upload Status Enum
class BatchUploadStatus(PyEnum):
    PENDING = "pending"
    PROCESSING = "processing"
    COMPLETED = "completed"
    PARTIALLY_COMPLETED = "partially_completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class BatchUploadItemStatus(PyEnum):
    PENDING = "pending"
    UPLOADING = "uploading"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class BatchUpload(Base):
    """Tracks bulk upload batches for processing multiple files at once"""

    __tablename__ = "BatchUpload"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("User.id"), nullable=False, index=True)
    client_id = Column(
        Integer, ForeignKey("Client.id", ondelete="SET NULL"), nullable=True
    )
    status = Column(
        Enum(BatchUploadStatus), nullable=False, default=BatchUploadStatus.PENDING
    )
    total_files = Column(Integer, nullable=False, default=0)
    completed_files = Column(Integer, nullable=False, default=0)
    failed_files = Column(Integer, nullable=False, default=0)
    cancelled_files = Column(Integer, nullable=False, default=0)
    created_at = Column(DateTime, default=datetime.now, nullable=False)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)

    # Relationships
    user = relationship("User", backref="batch_uploads")
    items = relationship(
        "BatchUploadItem", back_populates="batch", cascade="all, delete-orphan"
    )


class BatchUploadItem(Base):
    """Individual file within a batch upload"""

    __tablename__ = "BatchUploadItem"

    id = Column(Integer, primary_key=True)
    batch_id = Column(
        Integer,
        ForeignKey("BatchUpload.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    scan_id = Column(
        Integer, ForeignKey("ACRCloudScan.id", ondelete="SET NULL"), nullable=True
    )
    filename = Column(String, nullable=False)
    status = Column(
        Enum(BatchUploadItemStatus),
        nullable=False,
        default=BatchUploadItemStatus.PENDING,
    )
    error_message = Column(String, nullable=True)
    acrcloud_file_id = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.now, nullable=False)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)

    # Relationships
    batch = relationship("BatchUpload", back_populates="items")
    scan = relationship("ACRCloudScan")


class Songs(Base):
    """Global song database - one entry per song regardless of how many users have it"""

    __tablename__ = "Songs"

    id = Column(Integer, primary_key=True)
    spotify_track_id = Column(String, unique=True, nullable=False, index=True)
    isrc = Column(String, nullable=False, index=True)
    title = Column(String, nullable=False)
    artist = Column(String, nullable=False)
    album = Column(String, nullable=False)
    album_art = Column(String)
    release_date = Column(DateTime)
    last_synced_at = Column(DateTime)
    created_at = Column(DateTime, default=datetime.now, nullable=False)

    user_catalogs = relationship(
        "UserCatalog", back_populates="song", cascade="all, delete-orphan"
    )


class UserCatalog(Base):
    """User's personal catalog with royalty splits (user-specific data)"""

    __tablename__ = "UserCatalog"

    id = Column(Integer, primary_key=True)
    user_id = Column(
        Integer, ForeignKey("User.id", ondelete="CASCADE"), nullable=False, index=True
    )
    song_id = Column(
        Integer, ForeignKey("Songs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    client_id = Column(
        Integer, ForeignKey("Client.id", ondelete="SET NULL"), nullable=True, index=True
    )
    publishing_royalty = Column(Double)
    master_royalty = Column(Double)
    date_added = Column(DateTime, default=datetime.now, nullable=False)
    is_infringement = Column(Boolean, default=False)
    case_status = Column(
        String, nullable=True
    )  # null, "in_review", "resolved", "rejected"
    pro = Column(String, nullable=True)  # PRO affiliation: "ASCAP", "BMI", "SESAC", etc.
    publisher_type = Column(
        String, nullable=True
    )  # CWR publisher type: "E", "AM", "SE", "ES"

    # Relationships
    user = relationship("User", back_populates="user_catalog")
    song = relationship("Songs", back_populates="user_catalogs")
    client = relationship("Client", back_populates="catalog_items")


class StatsCache(Base):
    """Cache for Songstats API data to reduce API calls"""

    __tablename__ = "StatsCache"
    __table_args__ = (
        UniqueConstraint(
            "spotify_track_id", "date_added", name="uq_stats_cache_track_date"
        ),
    )

    id = Column(Integer, primary_key=True)
    spotify_track_id = Column(String, nullable=False, index=True)
    date_added = Column(Date, nullable=False, index=True)
    youtube_playcount = Column(BigInteger, nullable=False, default=0)
    spotify_playcount = Column(BigInteger, nullable=False, default=0)


class StatsSyncMetadata(Base):
    """Track when stats were last synced for each track"""

    __tablename__ = "StatsSyncMetadata"

    id = Column(Integer, primary_key=True)
    spotify_track_id = Column(String, nullable=False, unique=True, index=True)
    last_synced_at = Column(DateTime, nullable=False)
    last_synced_date = Column(
        Date, nullable=False
    )  # The most recent date we have data for
    created_at = Column(DateTime, default=datetime.now, nullable=False)


class RevenueStatement(Base):
    """Revenue statement uploaded by user"""

    __tablename__ = "RevenueStatement"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("User.id"), nullable=False)
    client_id = Column(
        Integer, ForeignKey("Client.id", ondelete="SET NULL"), nullable=True, index=True
    )
    filename = Column(String, nullable=False)
    upload_date = Column(DateTime, default=datetime.now, nullable=False)
    statement_date = Column(DateTime, nullable=True)  # Date from CSV, not upload date
    transaction_count = Column(Integer, nullable=False, default=0)
    total_amount = Column(Double, nullable=False, default=0.0)
    file_size = Column(String, nullable=True)
    profile_id = Column(String, nullable=True)  # Statement profile used for auto-detection

    # Relationships
    user = relationship("User", backref="revenue_statements")
    client = relationship("Client", back_populates="revenue_statements")
    transactions = relationship(
        "RevenueTransaction", back_populates="statement", cascade="all, delete-orphan"
    )


class RevenueTransaction(Base):
    """Individual transaction from a revenue statement"""

    __tablename__ = "RevenueTransaction"

    id = Column(Integer, primary_key=True)
    statement_id = Column(Integer, ForeignKey("RevenueStatement.id"), nullable=False)
    user_id = Column(Integer, ForeignKey("User.id"), nullable=False)

    # Transaction details
    date = Column(String, nullable=False)
    source = Column(String, nullable=False)
    source_category = Column(String, nullable=True)
    platform = Column(
        String, nullable=True
    )  # Streaming platform (Spotify, Apple Music, etc.)
    product = Column(String, nullable=True)
    territory = Column(String, nullable=True)
    territory_name = Column(String, nullable=True)
    currency = Column(String, nullable=False, default="USD")
    amount = Column(Double, nullable=False)
    artist = Column(String, nullable=True)
    writer = Column(String, nullable=True)
    status = Column(String, nullable=True, default="paid")

    # Optional fields from CSV
    category = Column(String, nullable=True)
    income_name = Column(String, nullable=True)
    income_period = Column(String, nullable=True)
    income_period_category = Column(String, nullable=True)
    isrc = Column(String, nullable=True, index=True)

    # Relationships
    statement = relationship("RevenueStatement", back_populates="transactions")
    user = relationship("User", backref="revenue_transactions")


# Pre-launch email signups
class PreLaunchSignup(Base):
    __tablename__ = "PreLaunchSignup"

    id = Column(Integer, primary_key=True, index=True)
    email = Column(String, unique=True, nullable=False, index=True)
    created_at = Column(DateTime, default=datetime.now, nullable=False)
    ip_address = Column(String, nullable=True)
    user_agent = Column(String, nullable=True)
    name = Column(String, nullable=True)
    company = Column(String, nullable=True)
    catalog_size = Column(String, nullable=True)
    role = Column(String, nullable=True)
    interest = Column(String, nullable=True)
    notes = Column(String, nullable=True)


class AgreementType(PyEnum):
    PRODUCER_AGREEMENT = "producer agreement"
    PUBLISHING = "publishing"
    MANAGEMENT = "management"

    def __str__(self):
        return str(self.value)


class Agreement(Base):
    """User's uploaded agreements (contracts, deals, etc.)"""

    __tablename__ = "Agreement"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("User.id"), nullable=False)
    filename = Column(String, nullable=False)
    original_filename = Column(String, nullable=False)
    file_size = Column(Integer, nullable=False)
    agreement_type = Column(Enum(AgreementType), nullable=False)
    uploaded_at = Column(DateTime, default=datetime.now, nullable=False)
    parsed_content = Column(
        JSON, nullable=True
    )  # Store extracted text/metadata + extraction_metadata
    analysis_version = Column(
        String, nullable=True, default="1.0"
    )  # Track analyzer version for schema evolution

    # Extraction metadata columns for efficient querying
    # These provide indexed access for filtering low-quality extractions
    extraction_method = Column(
        String(50), nullable=True
    )  # "vision_api", "pdfplumber", "pypdf2", "docx"
    extraction_quality_score = Column(
        Integer, nullable=True, default=0
    )  # 0-100 quality score
    extraction_warnings = Column(
        JSON, nullable=True, default=list
    )  # List of warning messages
    text_character_count = Column(
        Integer, nullable=True, default=0
    )  # Character count of extracted text

    # Persistent file storage for re-extraction
    # Stores path to original uploaded file (local: /storage/agreements/{uuid}{ext} or cloud URL)
    file_storage_path = Column(String(500), nullable=True)

    # Relationships
    user = relationship("User", backref="agreements")


class NotificationCategory(PyEnum):
    ATTENTION = "attention"
    FEED = "feed"

    def __str__(self):
        return str(self.value)


class NotificationPriority(PyEnum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"

    def __str__(self):
        return str(self.value)


class NotificationStatus(PyEnum):
    UNREAD = "unread"
    READ = "read"
    DISMISSED = "dismissed"
    RESOLVED = "resolved"

    def __str__(self):
        return str(self.value)


class Notification(Base):
    """User notifications - both actionable (attention) and informational (feed)"""

    __tablename__ = "Notification"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("User.id"), nullable=False, index=True)
    category = Column(Enum(NotificationCategory), nullable=False, index=True)
    type = Column(
        String, nullable=False, index=True
    )  # e.g., "revenue_discrepancy", "new_match", "weekly_report"
    title = Column(String, nullable=False)
    body = Column(String, nullable=True)
    priority = Column(
        Enum(NotificationPriority), nullable=True
    )  # Only for attention items
    status = Column(
        Enum(NotificationStatus), nullable=False, default=NotificationStatus.UNREAD
    )
    extra_data = Column(
        JSON, nullable=True
    )  # Store type-specific data (song_id, match_id, dollar amounts, etc.)
    action_url = Column(String, nullable=True)  # Where CTA button links to
    created_at = Column(DateTime, default=datetime.now, nullable=False, index=True)
    read_at = Column(DateTime, nullable=True)

    # Relationships
    user = relationship("User", backref="notifications")


class Client(Base):
    """Client/Artist that a user manages - for multi-client catalog and revenue management"""

    __tablename__ = "Client"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("User.id"), nullable=False, index=True)
    name = Column(String, nullable=False)
    avatar_url = Column(String, nullable=True)
    color = Column(
        String, nullable=True, default="#3b82f6"
    )  # Hex color for UI identification
    writer_ipi = Column(String, nullable=True)
    writer_name = Column(String, nullable=True)
    publisher_ipi = Column(String, nullable=True)
    publisher_name = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.now, nullable=False)
    updated_at = Column(
        DateTime, default=datetime.now, onupdate=datetime.now, nullable=False
    )

    # Relationships
    user = relationship("User", backref="clients")
    catalog_items = relationship("UserCatalog", back_populates="client")
    revenue_statements = relationship("RevenueStatement", back_populates="client")


class PushSubscription(Base):
    """Web Push subscriptions for real-time notifications"""

    __tablename__ = "PushSubscription"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("User.id"), nullable=False, index=True)
    endpoint = Column(String, nullable=False, unique=True)
    p256dh_key = Column(String, nullable=False)  # Client public key
    auth_key = Column(String, nullable=False)  # Auth secret
    user_agent = Column(String, nullable=True)  # Browser/device info
    created_at = Column(DateTime, default=datetime.now, nullable=False)
    last_used_at = Column(DateTime, nullable=True)  # Track when last push was sent

    # Relationships
    user = relationship("User", backref="push_subscriptions")


class DeviceToken(Base):
    """Mobile device tokens for APNs (iOS) and FCM (Android) push notifications"""

    __tablename__ = "DeviceToken"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("User.id"), nullable=False, index=True)
    token = Column(String, nullable=False, unique=True, index=True)
    platform = Column(String, nullable=False)  # "ios" or "android"
    bundle_id = Column(String, nullable=True)  # App bundle identifier
    device_name = Column(String, nullable=True)  # Optional device name
    created_at = Column(DateTime, default=datetime.now, nullable=False)
    last_used_at = Column(DateTime, nullable=True)

    # Relationships
    user = relationship("User", backref="device_tokens")


class PublishingAgreement(Base):
    """Records when a user agrees to let Verax collect their unclaimed royalties"""

    __tablename__ = "PublishingAgreement"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("User.id"), nullable=False, index=True)
    agreement_type = Column(String, nullable=False)  # "mechanical" or "performance"
    song_count = Column(Integer, nullable=False, default=0)
    songs_data = Column(JSON, nullable=True)  # Array of song details
    status = Column(
        String, nullable=False, default="pending"
    )  # pending, active, completed
    created_at = Column(DateTime, default=datetime.now, nullable=False)
    updated_at = Column(
        DateTime, default=datetime.now, onupdate=datetime.now, nullable=False
    )

    # Relationships
    user = relationship("User", backref="publishing_agreements")


class DocuSignStatus(PyEnum):
    DRAFT = "draft"
    SENT = "sent"
    WRITER_SIGNED = "writer_signed"
    COMPLETED = "completed"
    DECLINED = "declined"
    VOIDED = "voided"


class DocuSignPublishingAgreement(Base):
    """Tracks Publishing Administration Agreements sent via DocuSign"""

    __tablename__ = "DocuSignPublishingAgreement"

    id = Column(Integer, primary_key=True)

    signer_legal_name = Column(String, nullable=False)
    signer_producer_name = Column(String, nullable=False)
    signer_email = Column(String, nullable=False, index=True)
    signer_address = Column(String, nullable=False)
    signer_city = Column(String, nullable=False)
    signer_state = Column(String, nullable=False)
    signer_zip = Column(String, nullable=False)
    signer_country = Column(String, nullable=False, default="United States")

    docusign_envelope_id = Column(String, nullable=True, unique=True, index=True)
    status = Column(
        Enum(DocuSignStatus), nullable=False, default=DocuSignStatus.DRAFT
    )

    unsigned_pdf_path = Column(String(500), nullable=True)
    signed_pdf_path = Column(String(500), nullable=True)

    agreement_date = Column(DateTime, nullable=False)
    admin_fee_percent = Column(Integer, nullable=False, default=15)
    term_type = Column(String(20), nullable=False, default="3month")

    created_at = Column(DateTime, default=datetime.now, nullable=False)
    updated_at = Column(
        DateTime, default=datetime.now, onupdate=datetime.now, nullable=False
    )
    signed_at = Column(DateTime, nullable=True)
    pdf_delivered_at = Column(DateTime, nullable=True)

    user_id = Column(Integer, ForeignKey("User.id"), nullable=True, index=True)
    user = relationship("User", backref="docusign_publishing_agreements")


class Case(Base):
    """Filed cases for royalty claims and infringement"""

    __tablename__ = "Case"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("User.id", ondelete="CASCADE"), nullable=False, index=True)
    user_catalog_id = Column(Integer, ForeignKey("UserCatalog.id", ondelete="SET NULL"), nullable=True)
    client_id = Column(Integer, ForeignKey("Client.id", ondelete="SET NULL"), nullable=True)

    # Track info snapshot
    track_title = Column(String, nullable=False)
    track_artist = Column(String, nullable=True)
    track_isrc = Column(String, nullable=True)
    spotify_track_id = Column(String, nullable=True)

    # Case details
    selected_issues = Column(String, nullable=False)  # comma-separated
    publishing_split = Column(Integer, nullable=True)
    master_split = Column(Integer, nullable=True)
    estimated_publishing = Column(String, nullable=True)
    estimated_master = Column(String, nullable=True)
    estimated_total = Column(String, nullable=True)
    spotify_streams = Column(BigInteger, nullable=True)
    youtube_views = Column(BigInteger, nullable=True)

    # Status
    status = Column(String, nullable=False, default="in_review")  # in_review, in_the_works, closed
    notes = Column(String, nullable=True)

    created_at = Column(DateTime, default=datetime.now, nullable=False)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now, nullable=False)

    # Relationships
    user = relationship("User", backref="cases")
    user_catalog = relationship("UserCatalog", backref="cases")
    client = relationship("Client", backref="cases")


# Statement data integration domain (PRD §5) — registered on the same Base so
# Base.metadata / Alembic see these tables wherever models are imported.
from . import statements  # noqa: E402,F401
