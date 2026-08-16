"""Clients and commission partners are different populations.

The roster holds both. Counting them together and calling the total "clients"
is what made the dashboard report 876 active clients when the imported client
list holds 810 — the difference is commission partners, who are payees but not
clients. Some people are on both sheets, so the two totals overlap and must
never be added together.
"""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.database.session import get_session
from app.models.models import User
from app.models.statements import Publisher, Writer, WriterKind, WriterStatus
from app.routers.auth import get_user
from app.routers.writers_admin import writers_admin_router

ADMIN_EMAIL = "roster-admin@verax.app"


@pytest.fixture()
def client(session, monkeypatch):
    monkeypatch.setenv("ADMIN_EMAILS", ADMIN_EMAIL)
    admin = User(email=ADMIN_EMAIL, username="roster-admin", royalty_per_stream=0)
    session.add(admin)
    session.commit()
    app = FastAPI()
    app.include_router(writers_admin_router)
    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[get_user] = lambda: admin
    return TestClient(app)


@pytest.fixture()
def roster(session):
    """4 clients, 3 commission partners, 1 on both — 6 distinct people."""
    pub = Publisher(name="Regalias Digitales")
    session.add(pub)
    session.commit()

    def add(name, is_client, is_partner):
        w = Writer(
            canonical_name=name, publisher_id=pub.id, kind=WriterKind.CLIENT,
            status=WriterStatus.ACTIVE, is_client=is_client,
            is_commission_partner=is_partner,
        )
        session.add(w)
        return w

    add("Client One", True, False)
    add("Client Two", True, False)
    add("Client Three", True, False)
    add("On Both Sheets", True, True)      # counted in BOTH totals
    add("Partner One", False, True)
    add("Partner Two", False, True)
    session.commit()


def _total(client, **params):
    r = client.get("/admin/writers", params={"page_size": 50, **params})
    assert r.status_code == 200, r.text
    return r.json()["total"]


def test_unfiltered_total_is_every_payee_not_the_client_count(client, roster):
    """6 distinct people: calling this "clients" is the bug being fixed."""
    assert _total(client) == 6


def test_client_filter_returns_only_clients(client, roster):
    assert _total(client, membership="client") == 4


def test_commission_partner_filter_returns_only_partners(client, roster):
    assert _total(client, membership="commission_partner") == 3


def test_the_two_populations_overlap_and_must_not_be_summed(client, roster):
    """4 + 3 = 7 but only 6 people exist. Someone on both sheets is counted in
    each total, so presenting them as a sum overstates the roster."""
    clients = _total(client, membership="client")
    partners = _total(client, membership="commission_partner")
    everyone = _total(client)
    assert clients + partners > everyone
    assert everyone == 6


def test_any_is_the_same_as_no_filter(client, roster):
    assert _total(client, membership="any") == _total(client)


def test_an_unknown_membership_is_rejected_not_ignored(client, roster):
    """Silently ignoring a typo would report every payee as if it were the
    filtered population — exactly the wrong-number failure mode."""
    r = client.get("/admin/writers", params={"membership": "clients"})
    assert r.status_code == 400
