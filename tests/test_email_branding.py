"""Templated email carries the publisher's branding, not Verax's.

The shared HTML shell defaults to VERAX / "your Verax Team" / "Verax UG".
That is right for the Verax product and wrong for every other publisher
running this code: a Regalias Digitales writer asking for a password reset
received one signed "your Verax Team" above a Verax UG copyright line, for a
company they have never heard of.

The point of these tests is the pair of them: branding follows the deployment,
AND an unconfigured deployment still falls through to the template's own
defaults so Verax is untouched.
"""

import pytest

from app.emails.emails import EMail


def _mailer(from_name="Regalias Digitales"):
    m = EMail.__new__(EMail)
    m.from_name = from_name
    return m


@pytest.fixture()
def no_branding(monkeypatch):
    for key in ("email_brand_name", "email_signoff_name", "email_footer_text",
                "email_social_url", "email_social_label"):
        monkeypatch.setattr(f"app.emails.emails.settings.{key}", None, raising=False)


def test_brand_falls_back_to_the_sender_name(no_branding):
    """EMAIL_FROM_NAME is already set correctly on every deployment, so it is
    the sane default rather than requiring more configuration."""
    b = _mailer()._brand()
    assert b["brand_name"] == "Regalias Digitales"
    assert b["signoff_name"] == "the Regalias Digitales team"


def test_explicit_settings_win(no_branding, monkeypatch):
    monkeypatch.setattr("app.emails.emails.settings.email_brand_name", "RD Royalties", raising=False)
    monkeypatch.setattr("app.emails.emails.settings.email_footer_text", "Copyright 2026 RD", raising=False)
    b = _mailer()._brand()
    assert b["brand_name"] == "RD Royalties"
    assert b["footer_text"] == "Copyright 2026 RD"


def test_unset_keys_are_omitted_so_template_defaults_apply(no_branding):
    """Unset must leave the template's own defaults in place."""
    b = _mailer()._brand()
    assert "footer_text" not in b
    assert "social_url" not in b
    assert "social_label" not in b


def test_explicitly_empty_suppresses_rather_than_falling_back(no_branding, monkeypatch):
    """EMAIL_SOCIAL_URL="" means "no social link", NOT "use the default".

    Treating empty as unset is what left a Verax Instagram link in the footer
    of Regalias Digitales password resets: the blank was read as "unconfigured"
    so the vendor default came back.
    """
    monkeypatch.setattr("app.emails.emails.settings.email_social_url", "", raising=False)
    b = _mailer()._brand()
    assert b["social_url"] == ""        # present, and empty
    assert "social_label" not in b      # still unset, still defaulted


def test_no_sender_name_leaves_everything_to_the_template(no_branding):
    """A deployment with nothing configured must not inject blanks."""
    b = _mailer(from_name="")._brand()
    assert b == {}


def test_reset_password_email_is_not_signed_verax(no_branding, monkeypatch):
    """End to end through the real template: the rendered reset email must
    carry the publisher's name and never Verax's."""
    sent = {}

    class _P:
        name = "resend"

        def send(self, *, sender, to, subject, html, text, reply_to=None):
            sent["html"] = html
            return "id"

    monkeypatch.setattr("app.emails.emails.get_provider", lambda: _P())

    m = _mailer()
    m.from_email = "royalties@mail.regaliasdigitales.com"
    m.reply_to = "daniel@regaliasdigitales.com"
    m.server, m.port, m.email, m.password = "", 0, "", ""

    import os
    import jinja2
    from itsdangerous import URLSafeTimedSerializer
    import app.emails.emails as emails_mod

    m.serializer = URLSafeTimedSerializer("test-secret")
    m.jinja = jinja2.Environment()
    m.template_path = os.path.join(
        os.path.dirname(emails_mod.__file__), "templates", "email_template.html"
    )

    class _U:
        email = "writer@example.com"
        username = "writer"

    EMail.send_reset_password_email(m, _U())

    html = sent["html"]
    assert "Regalias Digitales" in html
    assert "your Verax Team" not in html
    assert "VERAX" not in html
