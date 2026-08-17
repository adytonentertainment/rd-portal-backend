"""Delivery is a config switch: a provider is used when configured, and the
SMTP mailbox is only the fallback. Nothing about the message changes with it.
"""

import pytest

import app.emails.emails as emails_mod
from app.emails.emails import EMail
from app.emails.providers import EmailSendError, get_provider


class _FakeSettings:
    def __init__(self, provider="smtp", key=None):
        self.email_provider = provider
        self.email_api_key = key


def _mailer():
    m = EMail.__new__(EMail)
    m.from_name = "Regalias Digitales"
    m.from_email = "royalties@regaliasdigitales.com"
    m.server = ""       # deliberately unset: a provider must not need SMTP
    m.port = 0
    m.email = ""
    m.password = ""
    return m


def test_provider_send_needs_no_smtp_settings(monkeypatch):
    """The guard that reports missing SMTP config must not fire when an HTTP
    provider is doing the sending."""
    sent = {}

    class _P:
        name = "resend"

        def send(self, *, sender, to, subject, html, text):
            sent.update(sender=sender, to=to, subject=subject, html=html)
            return "msg_123"

    monkeypatch.setattr(emails_mod, "get_provider", lambda: _P())
    EMail.send_email(_mailer(), "client@example.com", "client", "Subject", "<p>Body</p>")
    assert sent["to"] == "client@example.com"
    assert "royalties@regaliasdigitales.com" in sent["sender"]


def test_from_address_is_the_custom_domain(monkeypatch):
    """The visible sender is EMAIL_FROM, so pointing it at a verified domain is
    the whole of 'use my own address'."""
    sent = {}

    class _P:
        name = "resend"

        def send(self, *, sender, to, subject, html, text):
            sent["sender"] = sender
            return "id"

    monkeypatch.setattr(emails_mod, "get_provider", lambda: _P())
    EMail.send_email(_mailer(), "a@b.com", "a", "S", "<p>b</p>")
    assert sent["sender"] == "Regalias Digitales <royalties@regaliasdigitales.com>"


def test_unknown_provider_names_the_valid_ones():
    with pytest.raises(EmailSendError) as exc:
        get_provider(_FakeSettings(provider="mailchimp"))
    msg = str(exc.value)
    assert "mailchimp" in msg and "resend" in msg and "postmark" in msg


def test_provider_without_key_says_so():
    with pytest.raises(EmailSendError) as exc:
        get_provider(_FakeSettings(provider="resend", key=None))
    assert "EMAIL_API_KEY" in str(exc.value)


def test_default_is_smtp_so_nothing_changes_until_configured():
    assert get_provider(_FakeSettings()) is None
