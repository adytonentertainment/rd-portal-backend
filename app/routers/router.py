from fastapi import APIRouter
from .auth import auth_router
from .scan import scan_router
from .contact import contact_router
from .stripe import stripe_router
from .catalog import catalog_router
from .search import search_router
from .royalty import royalty_router
from .stats import stats_router
from .mlc_audit import mlc_audit_router
from .agreements import agreements_router
from .notifications import notifications_router
from .pro_audit import pro_audit_router
from .push import push_router, notifications_device_router
from .users import users_router
from .clients import clients_router
from .genius_stream import genius_stream_router
from .free_audit import free_audit_router
from .publishing_admin import publishing_admin_router
from .prelaunch import prelaunch_router
from .statements_admin import (
    distributions_admin_router,
    findings_admin_router,
    statements_admin_router,
)
from .clients_import_admin import client_import_admin_router
from .portal import me_router, portal_router, writer_invites_admin_router
from .writers_admin import writers_admin_router
from .accounts import accounts_router, admin_accounts_router

api_router = APIRouter()

# what does tags do? -> Title for swagger ui
api_router.include_router(auth_router)
api_router.include_router(scan_router)
api_router.include_router(contact_router)
api_router.include_router(stripe_router)
api_router.include_router(catalog_router)
api_router.include_router(search_router)
api_router.include_router(royalty_router)
api_router.include_router(stats_router)
api_router.include_router(mlc_audit_router)
api_router.include_router(agreements_router)
api_router.include_router(notifications_router)
api_router.include_router(pro_audit_router)
api_router.include_router(push_router)
api_router.include_router(notifications_device_router)  # iOS app compatibility
api_router.include_router(users_router)  # User settings including notification preferences
api_router.include_router(clients_router)  # Client/Artist management
api_router.include_router(genius_stream_router)  # SSE streaming for Genius fetch
api_router.include_router(free_audit_router)  # Public free audit (no auth)
api_router.include_router(publishing_admin_router)  # Publishing admin agreement signing
api_router.include_router(prelaunch_router)  # Demo request / signup
api_router.include_router(statements_admin_router)  # Publisher statement ingestion (admin)
api_router.include_router(findings_admin_router)  # Validation finding waive/acknowledge (admin)
api_router.include_router(distributions_admin_router)  # Distribution unpublish (admin)
api_router.include_router(client_import_admin_router)  # Client-list import + identity (admin)
api_router.include_router(writer_invites_admin_router)  # Bootstrap portal invites (admin)
api_router.include_router(writers_admin_router)  # Writer/Client Manager CRUD (admin)
api_router.include_router(me_router)  # Contact-scoped writer portal
api_router.include_router(portal_router)  # Public invite preview/accept
api_router.include_router(accounts_router)  # Role-aware account registration
api_router.include_router(admin_accounts_router)  # Admin approval workflow (admin)
