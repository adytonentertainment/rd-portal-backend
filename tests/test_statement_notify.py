"""'Notify writers': tell portal contacts that new statements are ready.

Publishing never emails. An admin notifies the newest period on purpose; older
(backfilled) periods are refused; each person gets one email in their
language, without amounts, and never twice.
"""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.database.session import get_session
from app.models.models import User
from app.models.statements import (
    Catalog,
    Contact,
    Distribution,
    Publisher,
    StatementNotification,
    Writer,
    WriterContact,
    WriterKind,
)
from app.routers.auth import get_user
from app.routers.statements_admin import distributions_admin_router
from app.services.portal import statement_notify

CURRENT = "PUB26H1"
OLD = "PUB25H2"
QUARTER = "PUB26Q2"


class Recorder:
    """Stands in for EMail: records what would have been sent."""

    def __init__(self, fail_for=(), blocked=()):
        self.sent = []
        self.fail_for = set(fail_for)
        self.blocked = set(blocked)

    def _blocked_by_allowlist(self, email):
        return email in self.blocked

    def send_statements_ready_email(self, **kw):
        if kw["recipient_email"] in self.fail_for:
            raise RuntimeError("mailbox unavailable")
        self.sent.append(kw)


@pytest.fixture()
def world(session):
    pub = Publisher(name="Regalias Digitales")
    session.add(pub)
    session.flush()

    def writer(name, **kw):
        w = Writer(publisher_id=pub.id, canonical_name=name, kind=WriterKind.CLIENT, **kw)
        session.add(w)
        session.flush()
        return w

    a, b, c = writer("Alpha"), writer("Bravo"), writer("Charlie", preferred_language="en")
    d = writer("Delta")  # contact listed but never accepted an invite
    e = writer("Echo Catalog", publisher_owned=True)  # acquired: never told

    def login(email, lang=None):
        u = User(email=email, username=email.split("@")[0], royalty_per_stream=0)
        session.add(u)
        session.flush()
        ct = Contact(email=email, user_id=u.id, preferred_language=lang)
        session.add(ct)
        session.flush()
        return u, ct

    u1, manager = login("manager@example.com", lang="es")  # holds Alpha + Bravo
    u2, charlie = login("charlie@example.com")
    for w in (a, b):
        session.add(WriterContact(writer_id=w.id, contact_id=manager.id, user_id=u1.id))
    session.add(WriterContact(writer_id=c.id, contact_id=charlie.id, user_id=u2.id))
    session.add(WriterContact(writer_id=e.id, contact_id=charlie.id, user_id=u2.id))
    listed = Contact(email="delta@example.com")
    session.add(listed)
    session.flush()
    session.add(WriterContact(writer_id=d.id, contact_id=listed.id))  # no user_id

    def publish(w, period, n=1):
        for _ in range(n):
            session.add(Distribution(statement_id=1, writer_id=w.id, batch_id=1,
                                     period_code=period, catalog=Catalog.YT, portal_visible=True))

    for w in (a, b, c, d, e):
        publish(w, CURRENT)
    publish(a, OLD)  # historical / backfilled
    publish(c, QUARTER)
    session.commit()
    return {"a": a, "b": b, "c": c, "d": d, "e": e, "manager": manager, "charlie": charlie}


def _notify(session, period=CURRENT, mailer=None):
    ids = statement_notify.start(session, period, admin_id=None)
    mailer = mailer or Recorder()
    statement_notify.send(ids, mailer=mailer, pacing=0)
    return mailer


def _notified(session, writer, period=CURRENT):
    return [d.notified_at for d in session.query(Distribution).filter_by(writer_id=writer.id, period_code=period)]


def test_only_the_newest_half_year_and_quarter_are_notifiable(session, world):
    assert statement_notify.notifiable_periods(session) == {CURRENT, QUARTER}


def test_old_backfilled_periods_are_refused(session, world):
    with pytest.raises(statement_notify.NotifyRefused, match="newest"):
        statement_notify.start(session, OLD, admin_id=None)
    assert session.query(StatementNotification).count() == 0


def test_plan_one_email_per_person_only_people_who_can_sign_in(session, world):
    recipients, without_login = statement_notify.plan(session, CURRENT)
    by_email = {r["contact"].email: r["writer_ids"] for r in recipients}
    assert by_email == {
        "manager@example.com": sorted([world["a"].id, world["b"].id]),
        "charlie@example.com": [world["c"].id],  # not the acquired Echo Catalog
    }
    assert without_login == [world["d"].id]


def test_notify_sends_and_marks_statements(session, world):
    mailer = _notify(session)
    assert len(mailer.sent) == 2
    manager = next(m for m in mailer.sent if m["recipient_email"] == "manager@example.com")
    assert manager["language"] == "es"
    assert sorted(manager["writer_names"]) == ["Alpha", "Bravo"]
    assert manager["period_code"] == CURRENT
    assert manager["portal_url"].endswith("/statements")
    for key in ("a", "b", "c"):
        assert all(_notified(session, world[key]))
    assert not any(_notified(session, world["d"]))  # no login yet
    assert not any(_notified(session, world["e"]))  # acquired catalog
    assert not any(_notified(session, world["a"], OLD))  # other periods untouched


def test_never_twice(session, world):
    _notify(session)
    with pytest.raises(statement_notify.NotifyRefused, match="no one"):
        statement_notify.start(session, CURRENT, admin_id=None)


def test_a_late_batch_only_reaches_what_is_new(session, world):
    _notify(session)
    session.add(Distribution(statement_id=2, writer_id=world["c"].id, batch_id=2,
                             period_code=CURRENT, catalog=Catalog.YT, portal_visible=True))
    session.commit()
    mailer = _notify(session)
    assert [m["recipient_email"] for m in mailer.sent] == ["charlie@example.com"]


def test_a_failure_is_recorded_and_can_be_retried(session, world):
    _notify(session, mailer=Recorder(fail_for={"charlie@example.com"}))
    row = session.query(StatementNotification).filter_by(email="charlie@example.com").one()
    assert row.status == "failed" and "mailbox" in row.error
    assert not any(_notified(session, world["c"]))
    ids = statement_notify.retry(session, CURRENT)
    mailer = Recorder()
    statement_notify.send(ids, mailer=mailer, pacing=0)
    assert [m["recipient_email"] for m in mailer.sent] == ["charlie@example.com"]
    assert all(_notified(session, world["c"]))


def test_allowlist_suppresses_without_marking_then_retry_sends(session, world):
    _notify(session, mailer=Recorder(blocked={"manager@example.com"}))
    row = session.query(StatementNotification).filter_by(email="manager@example.com").one()
    assert row.status == "suppressed"
    assert not any(_notified(session, world["a"]))
    ids = statement_notify.retry(session, CURRENT)
    mailer = Recorder()
    statement_notify.send(ids, mailer=mailer, pacing=0)
    assert [m["recipient_email"] for m in mailer.sent] == ["manager@example.com"]
    assert all(_notified(session, world["a"]))


def test_preview_goes_only_to_the_admin_and_marks_nothing(session, world):
    mailer = Recorder()
    out = statement_notify.send_preview(session, CURRENT, "admin@example.com", mailer=mailer)
    assert [m["recipient_email"] for m in mailer.sent] == ["admin@example.com", "admin@example.com"]
    assert [m["language"] for m in mailer.sent] == ["en", "es"]
    assert out["languages"] == ["en", "es"]
    assert session.query(StatementNotification).count() == 0
    assert not any(_notified(session, world["a"]))


def test_the_email_has_no_amounts_and_reads_in_spanish(monkeypatch):
    from app.emails.emails import EMail

    captured = {}
    monkeypatch.setattr(EMail, "send_email", lambda self, to, name, subject, html: captured.update(subject=subject, html=html))
    EMail().send_statements_ready_email(
        recipient_email="x@example.com", writer_names=["Alpha", "Bravo"], period_code=CURRENT,
        portal_url="https://portal.example/statements", sender="Regalias Digitales", language="es",
    )
    assert "Alpha y Bravo" in captured["subject"] and "enero–junio de 2026" in captured["subject"]
    assert "$" not in captured["html"]
    assert "https://portal.example/statements" in captured["html"]


# --- endpoints ---------------------------------------------------------------


@pytest.fixture()
def api(session, world, monkeypatch):
    admin = User(email="admin@example.com", username="admin", role="admin", admin_approved=True, royalty_per_stream=0)
    session.add(admin)
    session.commit()
    holder = {"user": admin}
    app = FastAPI()
    app.include_router(distributions_admin_router)
    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[get_user] = lambda: holder["user"]
    recorder = Recorder()
    monkeypatch.setattr(statement_notify, "EMail", lambda: recorder)
    return TestClient(app), holder, recorder


def test_endpoint_summary_notify_and_refusal(api, session, world):
    client, _, recorder = api
    summary = client.get(f"/admin/distributions/periods/{CURRENT}/notify").json()
    assert summary["notifiable"] is True
    assert summary["recipients"] == 2 and summary["writers_without_login"] == 1
    r = client.post(f"/admin/distributions/periods/{CURRENT}/notify")
    assert r.status_code == 200, r.text
    assert r.json()["queued"] == 2
    assert len(recorder.sent) == 2  # background task ran
    after = client.get(f"/admin/distributions/periods/{CURRENT}/notify").json()
    assert after["emails"]["sent"] == 2 and after["recipients"] == 0
    assert client.post(f"/admin/distributions/periods/{OLD}/notify").status_code == 409


def test_non_admins_cannot_notify(api, session, world):
    client, holder, _ = api
    holder["user"] = User(email="w@example.com", username="w", role="writer", royalty_per_stream=0)
    assert client.post(f"/admin/distributions/periods/{CURRENT}/notify").status_code == 403
