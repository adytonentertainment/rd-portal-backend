"""Replies to portal email reach a person.

The From address is pinned to the verified sending domain
(royalties@mail.regaliasdigitales.com) because DKIM requires it, and that
mailbox does not receive. So a writer who opens their invite and hits Reply —
which is exactly what someone confused about their royalties does — was
writing to an address that silently goes nowhere.

EMAIL_REPLY_TO points those replies at a real inbox without touching the From
address or the DKIM alignment that makes the mail deliverable.
"""

import pytest

from app.emails.providers import PostmarkProvider, ResendProvider, SendGridProvider


class _Captured(Exception):
    """Carries the request body out of the provider instead of sending it."""

    def __init__(self, payload):
        self.payload = payload


@pytest.fixture()
def capture(monkeypatch):
    """Intercept the HTTP POST every provider funnels through."""
    def _fake_post(url, *, headers=None, json=None, provider=None):
        raise _Captured(json)

    monkeypatch.setattr("app.emails.providers._post", _fake_post)

    def _send(prov, **kw):
        try:
            prov.send(sender="RD <royalties@mail.regaliasdigitales.com>",
                      to="writer@example.com", subject="s", html="<p>h</p>",
                      text="h", **kw)
        except _Captured as c:
            return c.payload
        raise AssertionError("provider did not post")

    return _send


def test_resend_sets_reply_to(capture):
    body = capture(ResendProvider("k"), reply_to="daniel@regaliasdigitales.com")
    assert body["reply_to"] == "daniel@regaliasdigitales.com"
    # The From must NOT change — it is what DKIM signs against.
    assert body["from"] == "RD <royalties@mail.regaliasdigitales.com>"


def test_sendgrid_sets_reply_to(capture):
    body = capture(SendGridProvider("k"), reply_to="daniel@regaliasdigitales.com")
    assert body["reply_to"] == {"email": "daniel@regaliasdigitales.com"}


def test_postmark_sets_reply_to(capture):
    body = capture(PostmarkProvider("k"), reply_to="daniel@regaliasdigitales.com")
    assert body["ReplyTo"] == "daniel@regaliasdigitales.com"


@pytest.mark.parametrize("prov,key", [
    (ResendProvider("k"), "reply_to"),
    (SendGridProvider("k"), "reply_to"),
    (PostmarkProvider("k"), "ReplyTo"),
])
def test_absent_when_not_configured(capture, prov, key):
    """Unset must send no reply-to field at all, not an empty one — an empty
    Reply-To header is worse than none, and this keeps every existing
    deployment behaving exactly as it did."""
    body = capture(prov)
    assert key not in body


def test_default_is_none(capture):
    """Callers that never pass reply_to keep working unchanged."""
    body = capture(ResendProvider("k"))
    assert "reply_to" not in body
    assert body["to"] == ["writer@example.com"]
