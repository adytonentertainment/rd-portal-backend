from fastapi import APIRouter
from .auth import auth_router
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
api_router.include_router(auth_router)  # Login, password reset, profile
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
