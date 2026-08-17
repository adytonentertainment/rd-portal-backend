"""The invite email as the client receives it.

This mail arrives cold and asks someone to click through to their money, so
three things decide whether it works: it names the publisher they already do
business with, it is written in their language (about a third of this roster is
Spanish-speaking), and it states when the link stops working.
"""

from datetime import datetime

import pytest

from app.emails.emails import EMail


@pytest.fixture()
def sent(monkeypatch):
    """Capture what send_email would have transmitted."""
    box = {}

    def _capture(self, to, name, subject, html):
        box.update(to=to, subject=subject, html=html)

    monkeypatch.setattr(EMail, "send_email", _capture)
    mailer = EMail.__new__(EMail)
    mailer.from_name = "Verax"
    mailer.template_path = EMail.__init__.__globals__["os"].path.join(
        EMail.__init__.__globals__["os"].path.dirname(
            EMail.__init__.__globals__["__file__"]
        ),
        "templates",
        "email_template.html",
    )
    import jinja2

    mailer.jinja = jinja2.Environment()
    return mailer, box


EXPIRY = datetime(2026, 8, 31, 12, 0, 0)


def test_english_invite_names_publisher_and_expiry(sent):
    mailer, box = sent
    EMail.send_portal_invite_email(
        mailer, "a@b.com", "B-Legit", "https://x/invite/tok",
        expires_at=EXPIRY, invited_by="Regalias Digitales", language="en",
    )
    assert "Regalias Digitales" in box["subject"]
    assert "B-Legit" in box["subject"]
    assert "Regalias Digitales has invited you" in box["html"]
    assert "August 31, 2026" in box["html"]
    assert "https://x/invite/tok" in box["html"]


def test_spanish_invite_is_actually_spanish(sent):
    mailer, box = sent
    EMail.send_portal_invite_email(
        mailer, "a@b.com", "Amenazzy", "https://x/invite/tok",
        expires_at=EXPIRY, invited_by="Regalias Digitales", language="es",
    )
    assert "portal de regalías" in box["subject"]
    assert "le invita a acceder" in box["html"]
    assert "Abrir su portal" in box["html"]
    # the date must not fall back to the server's English C locale
    assert "31 de agosto de 2026" in box["html"]
    assert "August" not in box["html"]


def test_unknown_language_falls_back_to_english(sent):
    """An invite in the wrong language still beats no invite."""
    mailer, box = sent
    EMail.send_portal_invite_email(
        mailer, "a@b.com", "X", "https://x/i", expires_at=EXPIRY, language="pt",
    )
    assert "Your royalty portal" in box["subject"]


def test_sender_defaults_when_publisher_unknown(sent):
    mailer, box = sent
    EMail.send_portal_invite_email(mailer, "a@b.com", "X", "https://x/i")
    assert "Verax" in box["subject"]


def test_no_expiry_line_when_no_expiry(sent):
    mailer, box = sent
    EMail.send_portal_invite_email(mailer, "a@b.com", "X", "https://x/i")
    assert "expires on" not in box["html"]


def test_mail_is_branded_as_the_publisher_not_the_vendor(sent):
    """The reader has a relationship with the publisher, not with the software.
    A VERAX wordmark over "your Verax Team" on a mail about someone's royalties
    is what gets it reported as phishing."""
    mailer, box = sent
    EMail.send_portal_invite_email(
        mailer, "a@b.com", "Amenazzy", "https://x/i",
        expires_at=EXPIRY, invited_by="Regalias Digitales", language="es",
    )
    html = box["html"]
    for leak in ("VERAX", "Verax UG", "Verax Team", "instagram.com/verax"):
        assert leak not in html, f"vendor branding leaked: {leak}"
    assert "Regalias Digitales" in html
    assert "el equipo de Regalias Digitales" in html
    assert "Todos los derechos reservados" in html


def test_signoff_is_localised(sent):
    mailer, box = sent
    EMail.send_portal_invite_email(
        mailer, "a@b.com", "X", "https://x/i", invited_by="RD", language="es")
    assert "Atentamente," in box["html"]
    assert "Kind regards" not in box["html"]
