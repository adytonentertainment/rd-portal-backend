from functools import lru_cache
from typing import Literal, Optional

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings.

    Only the fields the royalty portal actually uses are required. Everything
    else carries a default, because this codebase still declares credentials
    for TuneScan-era features the portal never calls — Spotify, MLC, AudD,
    AcoustID, ACRCloud, reCAPTCHA. Declaring those mandatory meant the app
    refused to start on a deployment that had no reason to hold them, failing
    with fifteen "Field required" errors before reaching any code.
    """
    mode: Literal["production", "development", "staging"] = "production"

    passphrase: str = ""

    secret_key: str
    secret_algorithm: str
    contact_email: str = ""
    # Optional so a managed host that only injects DATABASE_URL (Render, Heroku)
    # can boot; app/database/database.py falls back to it. Setting this wins.
    sqlalchemy_database_url: str = ""

    base_url_frontend: str = ""
    base_url_backend: str = ""

    acrcloud_access_key: str = ""
    acrcloud_access_secret: str = ""
    acrcloud_bearer_token: str = ""
    acrcloud_container_id: int = 0

    # Stripe Mode Configuration
    stripe_mode: Literal["test", "live"] = Field(default="test")

    # Test Mode Keys
    stripe_test_secret_key: Optional[str] = None
    stripe_test_public_key: Optional[str] = None
    stripe_test_webhook_secret: Optional[str] = None

    # Live Mode Keys
    stripe_live_secret_key: Optional[str] = None
    stripe_live_public_key: Optional[str] = None
    stripe_live_webhook_secret: Optional[str] = None

    # Computed fields for API access
    stripe_api_secret_key: Optional[str] = None
    stripe_api_public_key: Optional[str] = None
    stripe_webhook_secret: Optional[str] = None

    # Test Price IDs - Essential Tier
    stripe_test_price_essential_flat: Optional[str] = None
    stripe_test_price_essential_extra: Optional[str] = None
    stripe_test_price_essential_yearly: Optional[str] = None

    # Live Price IDs - Essential Tier
    stripe_live_price_essential_flat: Optional[str] = None
    stripe_live_price_essential_extra: Optional[str] = None
    stripe_live_price_essential_yearly: Optional[str] = None

    # Computed Price IDs - Essential Tier
    stripe_price_essential_flat: Optional[str] = None
    stripe_price_essential_extra: Optional[str] = None
    stripe_price_essential_yearly: Optional[str] = None
    essential_scans_limit: int = 0
    essential_catalog_limit: int = 0

    # Test Price IDs - Pro Tier
    stripe_test_price_pro_flat: Optional[str] = None
    stripe_test_price_pro_extra: Optional[str] = None
    stripe_test_price_pro_yearly: Optional[str] = None

    # Live Price IDs - Pro Tier
    stripe_live_price_pro_flat: Optional[str] = None
    stripe_live_price_pro_extra: Optional[str] = None
    stripe_live_price_pro_yearly: Optional[str] = None

    # Computed Price IDs - Pro Tier
    stripe_price_pro_flat: Optional[str] = None
    stripe_price_pro_extra: Optional[str] = None
    stripe_price_pro_yearly: Optional[str] = None
    pro_scans_limit: int = 0
    pro_catalog_limit: int = 0

    # Test Price IDs - Elite Tier
    stripe_test_price_elite_flat: Optional[str] = None
    stripe_test_price_elite_extra: Optional[str] = None
    stripe_test_price_elite_yearly: Optional[str] = None

    # Live Price IDs - Elite Tier
    stripe_live_price_elite_flat: Optional[str] = None
    stripe_live_price_elite_extra: Optional[str] = None
    stripe_live_price_elite_yearly: Optional[str] = None

    # Computed Price IDs - Elite Tier
    stripe_price_elite_flat: Optional[str] = None
    stripe_price_elite_extra: Optional[str] = None
    stripe_price_elite_yearly: Optional[str] = None
    elite_scans_limit: int = 0
    elite_catalog_limit: int = 0

    # Test Price IDs - Enterprise Tier
    stripe_test_price_enterprise_flat: Optional[str] = None
    stripe_test_price_enterprise_extra: Optional[str] = None
    stripe_test_price_enterprise_yearly: Optional[str] = None

    # Live Price IDs - Enterprise Tier
    stripe_live_price_enterprise_flat: Optional[str] = None
    stripe_live_price_enterprise_extra: Optional[str] = None
    stripe_live_price_enterprise_yearly: Optional[str] = None

    # Computed Price IDs - Enterprise Tier
    stripe_price_enterprise_flat: Optional[str] = None
    stripe_price_enterprise_extra: Optional[str] = None
    stripe_price_enterprise_yearly: Optional[str] = None
    enterprise_scans_limit: int = 0
    enterprise_catalog_limit: int = 0

    @field_validator("stripe_api_secret_key", mode="before")
    @classmethod
    def set_stripe_secret_key(cls, v, info):
        stripe_mode = info.data.get("stripe_mode", "test")
        if stripe_mode == "live":
            return info.data.get("stripe_live_secret_key")
        return info.data.get("stripe_test_secret_key")

    @field_validator("stripe_api_public_key", mode="before")
    @classmethod
    def set_stripe_public_key(cls, v, info):
        stripe_mode = info.data.get("stripe_mode", "test")
        if stripe_mode == "live":
            return info.data.get("stripe_live_public_key")
        return info.data.get("stripe_test_public_key")

    @field_validator("stripe_webhook_secret", mode="before")
    @classmethod
    def set_stripe_webhook_secret(cls, v, info):
        stripe_mode = info.data.get("stripe_mode", "test")
        if stripe_mode == "live":
            return info.data.get("stripe_live_webhook_secret")
        return info.data.get("stripe_test_webhook_secret")

    # Essential Price validators
    @field_validator("stripe_price_essential_flat", mode="before")
    @classmethod
    def set_stripe_price_essential_flat(cls, v, info):
        stripe_mode = info.data.get("stripe_mode", "test")
        if stripe_mode == "live":
            return info.data.get("stripe_live_price_essential_flat")
        return info.data.get("stripe_test_price_essential_flat")

    @field_validator("stripe_price_essential_extra", mode="before")
    @classmethod
    def set_stripe_price_essential_extra(cls, v, info):
        stripe_mode = info.data.get("stripe_mode", "test")
        if stripe_mode == "live":
            return info.data.get("stripe_live_price_essential_extra")
        return info.data.get("stripe_test_price_essential_extra")

    # Pro Price validators
    @field_validator("stripe_price_pro_flat", mode="before")
    @classmethod
    def set_stripe_price_pro_flat(cls, v, info):
        stripe_mode = info.data.get("stripe_mode", "test")
        if stripe_mode == "live":
            return info.data.get("stripe_live_price_pro_flat")
        return info.data.get("stripe_test_price_pro_flat")

    @field_validator("stripe_price_pro_extra", mode="before")
    @classmethod
    def set_stripe_price_pro_extra(cls, v, info):
        stripe_mode = info.data.get("stripe_mode", "test")
        if stripe_mode == "live":
            return info.data.get("stripe_live_price_pro_extra")
        return info.data.get("stripe_test_price_pro_extra")

    # Elite Price validators
    @field_validator("stripe_price_elite_flat", mode="before")
    @classmethod
    def set_stripe_price_elite_flat(cls, v, info):
        stripe_mode = info.data.get("stripe_mode", "test")
        if stripe_mode == "live":
            return info.data.get("stripe_live_price_elite_flat")
        return info.data.get("stripe_test_price_elite_flat")

    @field_validator("stripe_price_elite_extra", mode="before")
    @classmethod
    def set_stripe_price_elite_extra(cls, v, info):
        stripe_mode = info.data.get("stripe_mode", "test")
        if stripe_mode == "live":
            return info.data.get("stripe_live_price_elite_extra")
        return info.data.get("stripe_test_price_elite_extra")

    # Enterprise Price validators
    @field_validator("stripe_price_enterprise_flat", mode="before")
    @classmethod
    def set_stripe_price_enterprise_flat(cls, v, info):
        stripe_mode = info.data.get("stripe_mode", "test")
        if stripe_mode == "live":
            return info.data.get("stripe_live_price_enterprise_flat")
        return info.data.get("stripe_test_price_enterprise_flat")

    @field_validator("stripe_price_enterprise_extra", mode="before")
    @classmethod
    def set_stripe_price_enterprise_extra(cls, v, info):
        stripe_mode = info.data.get("stripe_mode", "test")
        if stripe_mode == "live":
            return info.data.get("stripe_live_price_enterprise_extra")
        return info.data.get("stripe_test_price_enterprise_extra")

    # Yearly Price validators
    @field_validator("stripe_price_essential_yearly", mode="before")
    @classmethod
    def set_stripe_price_essential_yearly(cls, v, info):
        stripe_mode = info.data.get("stripe_mode", "test")
        if stripe_mode == "live":
            return info.data.get("stripe_live_price_essential_yearly")
        return info.data.get("stripe_test_price_essential_yearly")

    @field_validator("stripe_price_pro_yearly", mode="before")
    @classmethod
    def set_stripe_price_pro_yearly(cls, v, info):
        stripe_mode = info.data.get("stripe_mode", "test")
        if stripe_mode == "live":
            return info.data.get("stripe_live_price_pro_yearly")
        return info.data.get("stripe_test_price_pro_yearly")

    @field_validator("stripe_price_elite_yearly", mode="before")
    @classmethod
    def set_stripe_price_elite_yearly(cls, v, info):
        stripe_mode = info.data.get("stripe_mode", "test")
        if stripe_mode == "live":
            return info.data.get("stripe_live_price_elite_yearly")
        return info.data.get("stripe_test_price_elite_yearly")

    @field_validator("stripe_price_enterprise_yearly", mode="before")
    @classmethod
    def set_stripe_price_enterprise_yearly(cls, v, info):
        stripe_mode = info.data.get("stripe_mode", "test")
        if stripe_mode == "live":
            return info.data.get("stripe_live_price_enterprise_yearly")
        return info.data.get("stripe_test_price_enterprise_yearly")

    genius_bearer_token: str = ""
    genius_client_id: str = ""
    genius_client_secret: str = ""

    spotify_client_id: str = ""
    spotify_client_secret: str = ""

    email_username: str = ""
    email_server: str = ""
    email_port: int = 0
    email_password: str = ""
    # The visible sender, independent of the SMTP login. Many providers let you
    # authenticate as one mailbox and send as another (an alias, or a shared
    # address like royalties@). Defaults to the login when unset.
    email_from: Optional[str] = None
    email_from_name: str = "Verax"
    # Delivery route: 'smtp' (the original mailbox) or a transactional provider
    # — resend / sendgrid / postmark. See app/emails/providers.py.
    email_provider: str = "smtp"
    email_api_key: Optional[str] = None

    google_api_key: str = ""
    recaptcha_site_key: str = ""

    apify_api_key: str = ""

    songstats_api_key: str = ""

    # Sample Detection API Keys
    audd_api_key: str = ""
    acoustid_key: str = ""

    # MLC (Mechanical Licensing Collective) API
    mlc_api_url: str = ""
    mlc_username: str = ""
    mlc_password: str = ""

    # Beta Configuration
    beta_signup_limit: Optional[int] = (
        None  # Set to limit signups (e.g., 50 for closed beta)
    )
    beta_require_passphrase: bool = True  # Set to False to allow any password
    beta_allowed_tiers: Optional[str] = (
        None  # Comma-separated list (e.g., "essential,pro")
    )

    # Pre-Launch Configuration
    pre_launch: bool = Field(default=False)  # Set to True to enable pre-launch mode

    # Swagger UI / API docs
    enable_docs: bool = Field(default=False)  # Set to True to enable /docs and /redoc

    # TuneScan Configuration
    tunescan_min_confidence: float = Field(
        default=50.0
    )  # Minimum confidence threshold (0-100)

    # AI Configuration (for document parsing)
    anthropic_api_key: Optional[str] = None

    # Web Push (VAPID) Configuration
    vapid_private_key: Optional[str] = None
    vapid_public_key: Optional[str] = None
    vapid_contact_email: Optional[str] = None

    # APNs (Apple Push Notification Service) Configuration
    apns_key_id: Optional[str] = None  # 10-character key ID from Apple
    apns_team_id: Optional[str] = None  # 10-character team ID from Apple
    apns_key_path: Optional[str] = None  # Path to .p8 key file
    apns_bundle_id: Optional[str] = None  # App bundle ID (e.g., adyton.VeraxApp)
    apns_use_sandbox: bool = True  # Use sandbox for development, False for production

    # DocuSign Configuration (legacy — kept for reference)
    docusign_integration_key: Optional[str] = None
    docusign_user_id: Optional[str] = None
    docusign_account_id: Optional[str] = None
    docusign_rsa_private_key_path: Optional[str] = None
    docusign_auth_server: str = "account-d.docusign.com"
    docusign_base_url: str = "https://demo.docusign.net/restapi"
    docusign_hmac_secret: Optional[str] = None
    docusign_admin_signer_name: Optional[str] = None
    docusign_admin_signer_email: Optional[str] = None

    # DocuSeal Configuration
    docuseal_api_token: Optional[str] = None


class ProductionSettings(Settings):
    model_config = SettingsConfigDict(env_file=".env.production")


class StagingSettings(Settings):
    model_config = SettingsConfigDict(env_file=".env.staging")


class DevelopmentSettings(Settings):
    model_config = SettingsConfigDict(env_file=".env.development")


@lru_cache
def get_settings():
    import os

    env = os.getenv("ENVIRONMENT") or os.getenv("environment")
    config_map = {
        "DEVELOPMENT": DevelopmentSettings,
        "STAGING": StagingSettings,
        "PRODUCTION": ProductionSettings,
    }
    if not env:
        raise ValueError(
            "You need to specify the environent type as an environment variable. "
            f"Possible types are: ({','.join(config_map.keys())})."
        )
    env = env.upper()
    return config_map.get(env)()
