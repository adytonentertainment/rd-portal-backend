"""An unconfigured mailer must name the missing setting.

With no SMTP host set this called SMTP("") and raised
"[Errno -2] Name or service not known" — a DNS error that reads like a network
fault and sends you debugging the wrong layer. The invite records that string
as its delivery_error, so the admin sees it too.
"""

import pytest

from app.emails.emails import EMail


@pytest.fixture()
def unconfigured(monkeypatch):
    mailer = EMail.__new__(EMail)
    mailer.server = ""
    mailer.port = 0
    mailer.email = ""
    mailer.password = ""
    mailer.from_email = ""
    mailer.from_name = ""
    return mailer


def test_error_names_the_missing_settings(unconfigured, monkeypatch):
    monkeypatch.setattr("app.emails.emails.get_provider", lambda *a, **k: None)
    with pytest.raises(Exception) as exc:
        EMail.send_email(unconfigured, "a@b.com", "A", "subject", "<p>body</p>")
    msg = str(exc.value)
    assert "EMAIL_SERVER" in msg
    assert "Name or service not known" not in msg


def test_error_says_the_invite_is_still_usable(unconfigured, monkeypatch):
    """The operator's next action is 'copy the link', so the error must say so."""
    monkeypatch.setattr("app.emails.emails.get_provider", lambda *a, **k: None)
    with pytest.raises(Exception) as exc:
        EMail.send_email(unconfigured, "a@b.com", "A", "subject", "<p>body</p>")
    assert "copy its link" in str(exc.value).lower()
