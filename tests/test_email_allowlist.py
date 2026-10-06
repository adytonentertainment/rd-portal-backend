"""EMAIL_ALLOWLIST stops a not-yet-ready deployment contacting real clients.

The portal was live and reachable before it was ready for writers: public
registration was open, password resets worked, and one real invite had already
gone out to a client whose statement titles were still mis-encoded. An invite
cannot be un-sent, so "be careful" is not a control — this is.

While the allowlist is set, mail goes only to the listed addresses and is
dropped for everyone else. Clearing it restores normal delivery, so going live
is the absence of a setting rather than a code change.
"""

import pytest

from app.emails.emails import EMail


def _mailer():
    m = EMail.__new__(EMail)
    m.from_name = "Regalias Digitales"
    m.from_email = "royalties@mail.regaliasdigitales.com"
    m.reply_to = "daniel@regaliasdigitales.com"
    m.server, m.port, m.email, m.password = "", 0, "", ""
    return m


@pytest.fixture()
def sent(monkeypatch):
    """Capture whatever reaches the provider."""
    box = []

    class _P:
        name = "resend"

        def send(self, *, sender, to, subject, html, text, reply_to=None):
            box.append(to)
            return "id"

    monkeypatch.setattr("app.emails.emails.get_provider", lambda: _P())
    return box


def _set(monkeypatch, value):
    monkeypatch.setattr("app.emails.emails.settings.email_allowlist", value, raising=False)


def test_unset_sends_to_everyone(sent, monkeypatch):
    """The default must be normal delivery, so production is unaffected."""
    _set(monkeypatch, None)
    EMail.send_email(_mailer(), "writer@example.com", "W", "S", "<p>b</p>")
    assert sent == ["writer@example.com"]


def test_blank_is_treated_as_unset(sent, monkeypatch):
    _set(monkeypatch, "   ")
    EMail.send_email(_mailer(), "writer@example.com", "W", "S", "<p>b</p>")
    assert sent == ["writer@example.com"]


def test_listed_address_still_receives(sent, monkeypatch):
    _set(monkeypatch, "steven@adytonentertainment.com")
    EMail.send_email(_mailer(), "steven@adytonentertainment.com", "S", "S", "<p>b</p>")
    assert sent == ["steven@adytonentertainment.com"]


def test_everyone_else_is_dropped(sent, monkeypatch):
    """The whole point: a real writer must not receive anything."""
    _set(monkeypatch, "steven@adytonentertainment.com")
    EMail.send_email(_mailer(), "writer@regaliasdigitales.com", "W", "S", "<p>b</p>")
    assert sent == []


def test_matching_ignores_case_and_whitespace(sent, monkeypatch):
    _set(monkeypatch, " Steven@AdytonEntertainment.com , other@x.com ")
    EMail.send_email(_mailer(), "steven@adytonentertainment.com", "S", "S", "<p>b</p>")
    EMail.send_email(_mailer(), "OTHER@X.COM", "O", "S", "<p>b</p>")
    assert len(sent) == 2


def test_a_near_miss_is_not_allowed_through(sent, monkeypatch):
    """Substring matching would let daniel@regaliasdigitales.com.evil.com past."""
    _set(monkeypatch, "steven@adytonentertainment.com")
    for addr in (
        "steven@adytonentertainment.com.attacker.net",
        "notsteven@adytonentertainment.com",
        "steven@adytonentertainment.co",
    ):
        EMail.send_email(_mailer(), addr, "X", "S", "<p>b</p>")
    assert sent == []


def test_invite_path_is_covered_too(sent, monkeypatch):
    """Every email funnels through send_email, so the invite is caught by the
    same guard rather than needing its own."""
    _set(monkeypatch, "steven@adytonentertainment.com")
    m = _mailer()
    assert m._blocked_by_allowlist("writer@example.com") is True
    assert m._blocked_by_allowlist("steven@adytonentertainment.com") is False
