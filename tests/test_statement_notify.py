"""'Notify writers': tell portal contacts that new statements are ready.

Publishing never emails. An admin notifies the NEWEST RELEASE (every period
ending in the latest month: H1 2026 + Q2 2026) on purpose. Older or backfilled
periods can never be notified. Once per writer and once per person per release;
one email per person, in their language, without amounts.
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

H1 = "PUB26H1"
Q2 = "PUB26Q2"
OLD = "PUB25H2"


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
    W = {}

    def writer(key, name, **kw):
        w = Writer(publisher_id=pub.id, canonical_name=name, kind=WriterKind.CLIENT, **kw)
        session.add(w)
        session.flush()
        W[key] = w
        return w

    def login(email, lang=None):
        u = User(email=email, username=email.split("@")[0], royalty_per_stream=0)
        session.add(u)
        session.flush()
        ct = Contact(email=email, user_id=u.id, preferred_language=lang)
        session.add(ct)
        session.flush()
        W[email] = ct
        return u, ct

    def claim(w, user, contact):
        session.add(WriterContact(writer_id=w.id, contact_id=contact.id, user_id=user.id))

    def publish(w, period):
        session.add(Distribution(statement_id=1, writer_id=w.id, batch_id=1,
                                 period_code=period, catalog=Catalog.YT, portal_visible=True))

    a, b, c = writer("a", "Alpha"), writer("b", "Bravo"), writer("c", "Charlie")
    d = writer("d", "Delta")  # listed contact, never accepted an invite
    e = writer("e", "Echo Catalog", publisher_owned=True)  # acquired: never told
    q = writer("q", "Quarterly Only")  # only has the quarter

    u_mgr, mgr = login("manager@example.com", lang="es")  # holds Alpha + Bravo
    u_ch, ch = login("charlie@example.com")
    u_q, qc = login("quarterly@example.com")
    claim(a, u_mgr, mgr)
    claim(b, u_mgr, mgr)
    claim(c, u_ch, ch)
    claim(e, u_ch, ch)
    claim(q, u_q, qc)
    listed = Contact(email="delta@example.com")
    session.add(listed)
    session.flush()
    session.add(WriterContact(writer_id=d.id, contact_id=listed.id))  # no user_id

    for w in (a, b, c, d, e):
        publish(w, H1)
    publish(c, Q2)
    publish(q, Q2)
    publish(a, OLD)  # historical
    session.commit()
    W["publish"] = publish
    W["claim"] = claim
    W["login"] = login
    W["writer"] = writer
    return W


def _notify(session, mailer=None):
    ids = statement_notify.start(session, admin_id=None)
    mailer = mailer or Recorder()
    statement_notify.send(ids, mailer=mailer, pacing=0)
    return mailer


def _notified(session, writer, period=H1):
    return [d.notified_at for d in session.query(Distribution).filter_by(writer_id=writer.id, period_code=period)]


def test_the_newest_release_is_every_period_ending_in_the_latest_month(session, world):
    assert statement_notify.newest_release(session) == [H1, Q2]  # widest first
    assert OLD not in statement_notify.notifiable_periods(session)


def test_plan_one_email_per_person_including_the_quarterly_accounts(session, world):
    codes, recipients, without_login, covered = statement_notify.plan(session)
    by_email = {r["contact"].email: (r["writer_ids"], r["period_code"]) for r in recipients}
    assert by_email == {
        "manager@example.com": (sorted([world["a"].id, world["b"].id]), H1),
        "charlie@example.com": ([world["c"].id], H1),  # holds H1 and Q2: the half-year covers both
        "quarterly@example.com": ([world["q"].id], Q2),  # quarter-only wording
    }
    assert without_login == [world["d"].id]
    assert covered == {}


def test_notify_sends_once_per_person_and_marks_the_release(session, world):
    mailer = _notify(session)
    assert len(mailer.sent) == 3
    manager = next(m for m in mailer.sent if m["recipient_email"] == "manager@example.com")
    assert manager["language"] == "es" and sorted(manager["writer_names"]) == ["Alpha", "Bravo"]
    assert manager["portal_url"].endswith("/statements")
    for key in ("a", "b"):
        assert all(_notified(session, world[key]))
    assert all(_notified(session, world["c"], H1)) and all(_notified(session, world["c"], Q2))
    assert all(_notified(session, world["q"], Q2))
    assert not any(_notified(session, world["d"]))  # no login yet
    assert not any(_notified(session, world["e"]))  # acquired catalog
    assert not any(_notified(session, world["a"], OLD))  # historical untouched


def test_pressing_again_sends_nothing(session, world):
    _notify(session)
    with pytest.raises(statement_notify.NotifyRefused, match="already been notified"):
        statement_notify.start(session, admin_id=None)


def test_a_late_batch_does_not_email_again(session, world):
    _notify(session)
    world["publish"](world["c"], H1)  # e.g. the YouTube batch after Mechanical
    session.commit()
    _, recipients, _, _ = statement_notify.plan(session)
    assert recipients == []
    with pytest.raises(statement_notify.NotifyRefused):
        statement_notify.start(session, admin_id=None)


def test_a_person_is_never_emailed_twice_in_a_release(session, world):
    _notify(session)
    # A new client for the same manager gets statements later in the release.
    g = world["writer"]("g", "Golf")
    user = session.query(User).filter_by(email="manager@example.com").one()
    world["claim"](g, user, world["manager@example.com"])
    world["publish"](g, H1)
    session.commit()
    ids = statement_notify.start(session, admin_id=None)
    assert ids == []  # nobody emailed...
    assert all(_notified(session, g))  # ...and Golf is marked done for the release


def test_uploading_old_data_never_creates_a_notification(session, world):
    _notify(session)
    world["publish"](world["b"], OLD)  # backfill an older period
    session.commit()
    assert statement_notify.newest_release(session) == [H1, Q2]
    _, recipients, _, _ = statement_notify.plan(session)
    assert recipients == []
    assert not any(_notified(session, world["b"], OLD))


def test_a_newer_period_moves_the_release_forward(session, world):
    world["publish"](world["q"], "PUB26Q3")
    session.commit()
    assert statement_notify.newest_release(session) == ["PUB26Q3"]
    _, recipients, _, _ = statement_notify.plan(session)
    assert [r["contact"].email for r in recipients] == ["quarterly@example.com"]


def test_a_failure_is_recorded_and_can_be_retried(session, world):
    _notify(session, mailer=Recorder(fail_for={"charlie@example.com"}))
    row = session.query(StatementNotification).filter_by(email="charlie@example.com").one()
    assert row.status == "failed" and "mailbox" in row.error
    assert not any(_notified(session, world["c"]))
    ids = statement_notify.retry(session)
    mailer = Recorder()
    statement_notify.send(ids, mailer=mailer, pacing=0)
    assert [m["recipient_email"] for m in mailer.sent] == ["charlie@example.com"]
    assert all(_notified(session, world["c"]))


def test_allowlist_holds_without_marking_then_retry_sends(session, world):
    _notify(session, mailer=Recorder(blocked={"manager@example.com"}))
    row = session.query(StatementNotification).filter_by(email="manager@example.com").one()
    assert row.status == "suppressed"
    assert not any(_notified(session, world["a"]))
    ids = statement_notify.retry(session)
    mailer = Recorder()
    statement_notify.send(ids, mailer=mailer, pacing=0)
    assert [m["recipient_email"] for m in mailer.sent] == ["manager@example.com"]
    assert all(_notified(session, world["a"]))


def test_preview_goes_only_to_the_admin_and_marks_nothing(session, world):
    mailer = Recorder()
    out = statement_notify.send_preview(session, "admin@example.com", mailer=mailer)
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
        recipient_email="x@example.com", writer_names=["Alpha", "Bravo"], period_code=H1,
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


def test_endpoint_summary_notify_and_second_press(api, session, world):
    client, _, recorder = api
    summary = client.get("/admin/distributions/notify").json()
    assert summary["periods"] == [H1, Q2] and summary["label"] == "H1 2026 + Q2 2026"
    assert summary["recipients"] == 3 and summary["writers_without_login"] == 1
    r = client.post("/admin/distributions/notify")
    assert r.status_code == 200, r.text
    assert r.json()["queued"] == 3
    assert len(recorder.sent) == 3  # background task ran
    after = client.get("/admin/distributions/notify").json()
    assert after["emails"]["sent"] == 3 and after["recipients"] == 0
    assert client.post("/admin/distributions/notify").status_code == 409


def test_non_admins_cannot_notify(api, session, world):
    client, holder, _ = api
    holder["user"] = User(email="w@example.com", username="w", role="writer", royalty_per_stream=0)
    assert client.post("/admin/distributions/notify").status_code == 403


# --- once per person, even through holds, failures and retries ----------------


def test_pressing_again_after_a_hold_does_not_duplicate_the_person(session, world):
    _notify(session, mailer=Recorder(blocked={"manager@example.com"}))
    _notify(session, mailer=Recorder(blocked={"manager@example.com"}))  # held again
    rows = session.query(StatementNotification).filter_by(email="manager@example.com").all()
    assert [r.status for r in rows] == ["suppressed"]  # one row, refreshed
    ids = statement_notify.retry(session)
    mailer = Recorder()
    statement_notify.send(ids, mailer=mailer, pacing=0)
    assert [m["recipient_email"] for m in mailer.sent] == ["manager@example.com"]  # exactly once


def test_retry_queues_only_one_row_per_person(session, world):
    from app.models.statements import Contact

    mgr = session.query(Contact).filter_by(email="manager@example.com").one()
    for _ in range(2):  # legacy duplicates, e.g. from before this guard existed
        session.add(StatementNotification(period_code=H1, contact_id=mgr.id, email=mgr.email, language="es",
                                          writer_ids=[world["a"].id], distribution_ids=[], status="suppressed"))
    session.commit()
    ids = statement_notify.retry(session)
    assert len(ids) == 1
    statuses = sorted(r.status for r in session.query(StatementNotification).filter_by(contact_id=mgr.id))
    assert statuses == ["pending", "replaced"]


def test_the_sender_never_emails_a_person_twice_in_a_release(session, world):
    from app.models.statements import Contact

    mgr = session.query(Contact).filter_by(email="manager@example.com").one()
    rows = [StatementNotification(period_code=H1, contact_id=mgr.id, email=mgr.email, language="es",
                                  writer_ids=[world["a"].id], distribution_ids=[], status="pending")
            for _ in range(2)]
    session.add_all(rows)
    session.commit()
    mailer = Recorder()
    statement_notify.send([r.id for r in rows], mailer=mailer, pacing=0)
    assert len(mailer.sent) == 1
    assert sorted(r.status for r in rows) == ["replaced", "sent"]


# --- audit fixes: concurrency, restarts, escaping -----------------------------


def test_a_row_is_sent_once_even_if_two_senders_run(session, world):
    ids = statement_notify.start(session, admin_id=None)
    mailer = Recorder()
    statement_notify.send(ids, mailer=mailer, pacing=0)
    statement_notify.send(ids, mailer=mailer, pacing=0)  # a second sender with the same rows
    assert len(mailer.sent) == 3  # not 6


def test_a_row_claimed_by_another_sender_blocks_a_duplicate(session, world):
    from app.models.statements import Contact

    mgr = session.query(Contact).filter_by(email="manager@example.com").one()
    other = StatementNotification(period_code=H1, contact_id=mgr.id, email=mgr.email, language="es",
                                  writer_ids=[world["a"].id], distribution_ids=[], status="sending")
    mine = StatementNotification(period_code=H1, contact_id=mgr.id, email=mgr.email, language="es",
                                 writer_ids=[world["a"].id], distribution_ids=[], status="pending")
    session.add_all([other, mine])
    session.commit()
    mailer = Recorder()
    statement_notify.send([mine.id], mailer=mailer, pacing=0)
    assert mailer.sent == [] and mine.status == "replaced"


def test_rows_abandoned_by_a_restart_are_reused_not_duplicated(session, world):
    from datetime import datetime, timedelta

    from app.models.statements import Contact

    mgr = session.query(Contact).filter_by(email="manager@example.com").one()
    stuck = StatementNotification(period_code=H1, contact_id=mgr.id, email=mgr.email, language="es",
                                  writer_ids=[world["a"].id], distribution_ids=[], status="sending",
                                  created_at=datetime.now() - timedelta(hours=2))
    session.add(stuck)
    session.commit()
    assert statement_notify.summary(session)["sending"] is False  # a dead run does not block
    statement_notify.start(session, admin_id=None)
    rows = session.query(StatementNotification).filter_by(contact_id=mgr.id).all()
    assert len(rows) == 1 and rows[0].id == stuck.id and rows[0].status == "pending"


def test_names_cannot_inject_markup_into_either_email(monkeypatch):
    from app.emails.emails import EMail

    captured = {}
    monkeypatch.setattr(EMail, "send_email", lambda self, to, name, subject, html: captured.update(html=html))
    evil = '<a href="https://evil.example">Click</a>'
    EMail().send_statements_ready_email(recipient_email="x@example.com", writer_names=[evil], period_code=H1,
                                        portal_url="https://p.example/statements", sender="RD", language="en")
    assert evil not in captured["html"] and "&lt;a href=" in captured["html"]
    EMail().send_portal_invite_email(recipient_email="x@example.com", writer_name=evil,
                                     accept_url="https://p.example/invite/t", invited_by="RD", language="en")
    assert evil not in captured["html"] and "&lt;a href=" in captured["html"]
