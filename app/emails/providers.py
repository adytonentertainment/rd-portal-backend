"""Where outbound mail actually goes.

The app started on a single IONOS mailbox over SMTP. That is fine for a
handful of password resets and wrong for inviting 810 clients: a personal
mailbox has send-rate caps, no bounce feedback, and reputation you cannot see
until you are already in everyone's spam folder.

So delivery is a config switch. `EMAIL_PROVIDER` picks one:

    smtp      (default)  the original mailbox — unchanged behaviour
    resend                api.resend.com
    sendgrid              api.sendgrid.com
    postmark              api.postmarkapp.com

Every transactional provider also offers a plain SMTP endpoint, so if a
provider is ever not listed here it can still be used by pointing the SMTP
settings at it. The HTTP APIs are preferred: one request instead of a TLS
handshake plus AUTH per message, and a provider-side message id to chase when
a client says nothing arrived.

Every provider returns a `message_id` on success and raises `EmailSendError`
with the provider's own words on failure — those words are what makes a
delivery problem diagnosable ("domain not verified" vs "invalid key").
"""

from __future__ import annotations

from typing import Optional

import requests

from app.settings import get_settings

# Sending is not worth hanging a background worker over.
TIMEOUT_SECONDS = 20


class EmailSendError(Exception):
    """The provider refused the message. Carries their explanation verbatim."""


def _post(url: str, *, headers: dict, json: dict, provider: str) -> requests.Response:
    try:
        resp = requests.post(url, headers=headers, json=json, timeout=TIMEOUT_SECONDS)
    except requests.RequestException as exc:
        raise EmailSendError(f"{provider}: could not be reached ({exc})") from exc
    if resp.status_code >= 400:
        # The body is the useful part — "domain is not verified", "API key
        # does not have permission" — so surface it rather than the status.
        raise EmailSendError(f"{provider} rejected the message ({resp.status_code}): {resp.text[:300]}")
    return resp


class ResendProvider:
    name = "resend"

    def __init__(self, api_key: str):
        self.api_key = api_key

    def send(self, *, sender: str, to: str, subject: str, html: str, text: str) -> str:
        resp = _post(
            "https://api.resend.com/emails",
            headers={"Authorization": f"Bearer {self.api_key}"},
            json={"from": sender, "to": [to], "subject": subject, "html": html, "text": text},
            provider=self.name,
        )
        return (resp.json() or {}).get("id", "")


class SendGridProvider:
    name = "sendgrid"

    def __init__(self, api_key: str):
        self.api_key = api_key

    def send(self, *, sender: str, to: str, subject: str, html: str, text: str) -> str:
        name, address = _split_sender(sender)
        resp = _post(
            "https://api.sendgrid.com/v3/mail/send",
            headers={"Authorization": f"Bearer {self.api_key}"},
            json={
                "personalizations": [{"to": [{"email": to}]}],
                "from": {"email": address, "name": name} if name else {"email": address},
                "subject": subject,
                "content": [
                    # SendGrid requires plain text before html.
                    {"type": "text/plain", "value": text},
                    {"type": "text/html", "value": html},
                ],
            },
            provider=self.name,
        )
        return resp.headers.get("X-Message-Id", "")


class PostmarkProvider:
    name = "postmark"

    def __init__(self, api_key: str):
        self.api_key = api_key

    def send(self, *, sender: str, to: str, subject: str, html: str, text: str) -> str:
        resp = _post(
            "https://api.postmarkapp.com/email",
            headers={
                "X-Postmark-Server-Token": self.api_key,
                "Accept": "application/json",
            },
            json={
                "From": sender,
                "To": to,
                "Subject": subject,
                "HtmlBody": html,
                "TextBody": text,
                "MessageStream": "outbound",
            },
            provider=self.name,
        )
        return (resp.json() or {}).get("MessageID", "")


_PROVIDERS = {
    "resend": ResendProvider,
    "sendgrid": SendGridProvider,
    "postmark": PostmarkProvider,
}


def _split_sender(sender: str):
    """'Verax <a@b.com>' -> ('Verax', 'a@b.com'); 'a@b.com' -> (None, 'a@b.com')."""
    if "<" in sender and sender.rstrip().endswith(">"):
        name, _, addr = sender.partition("<")
        return name.strip().strip('"') or None, addr.rstrip(">").strip()
    return None, sender.strip()


def get_provider(settings=None) -> Optional[object]:
    """The configured HTTP provider, or None to fall back to SMTP."""
    settings = settings or get_settings()
    choice = (getattr(settings, "email_provider", None) or "smtp").strip().lower()
    if choice in ("", "smtp"):
        return None
    cls = _PROVIDERS.get(choice)
    if cls is None:
        raise EmailSendError(
            f"Unknown EMAIL_PROVIDER '{choice}'. Use one of: "
            f"smtp, {', '.join(sorted(_PROVIDERS))}."
        )
    key = getattr(settings, "email_api_key", None)
    if not key:
        raise EmailSendError(f"EMAIL_PROVIDER is '{choice}' but EMAIL_API_KEY is not set.")
    return cls(key)
