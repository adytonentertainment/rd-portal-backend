import os
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formatdate
from functools import lru_cache
from smtplib import SMTP
from urllib.parse import urljoin

import jinja2
from app.models import User
from app.emails.providers import get_provider
from app.settings import get_settings
from itsdangerous import URLSafeTimedSerializer

settings = get_settings()


class EMail:
    # uses the same api as the gmail service
    def __init__(self):
        # connect to smtp server
        self.email = settings.email_username
        self.server = settings.email_server
        self.port = settings.email_port
        self.password = settings.email_password
        # Who the recipient sees. Configurable separately from the SMTP login so
        # the sending address can change without touching code — set EMAIL_FROM
        # and EMAIL_FROM_NAME. Note the envelope sender stays the authenticated
        # mailbox; a From: the provider has not authorised will fail SPF/DKIM.
        self.from_email = settings.email_from or settings.email_username
        self.from_name = settings.email_from_name

        self.serializer = URLSafeTimedSerializer(settings.secret_key)
        self.jinja = jinja2.Environment()

        # Template paths (use absolute path to work on any environment)
        self.template_path = os.path.join(os.path.dirname(__file__), "templates", "email_template.html")
        self.activation_template_path = os.path.join(os.path.dirname(__file__), "templates", "activation_email_template.html")

    def _strip_html_to_plain(self, html):
        """Convert HTML to plain text for multipart/alternative."""
        import re
        text = re.sub(r'<br\s*/?>', '\n', html)
        text = re.sub(r'<a[^>]+href="([^"]*)"[^>]*>[^<]*</a>', r'\1', text)
        text = re.sub(r'<[^>]+>', '', text)
        text = re.sub(r'&nbsp;', ' ', text)
        text = re.sub(r'&amp;', '&', text)
        text = re.sub(r'&copy;', '(c)', text)
        text = re.sub(r'&quot;', '"', text)
        text = re.sub(r'&#\d+;', '', text)
        text = re.sub(r'\n{3,}', '\n\n', text)
        return text.strip()

    def send_email(self, receiver_email, receiver_name, subject, message):
        import uuid
        to_addr = f"{receiver_name} <{receiver_email}>"
        from_addr = f"{self.from_name} <{self.from_email}>"

        # Extract sender domain for Message-ID
        sender_domain = (
            self.from_email.split("@")[-1] if "@" in self.from_email else "verax.app"
        )

        # Create multipart/alternative message (plain text + HTML)
        mimeMessage = MIMEMultipart("alternative")
        mimeMessage["To"] = to_addr
        mimeMessage["Subject"] = subject
        mimeMessage["From"] = from_addr
        mimeMessage["Reply-To"] = f"{self.from_name} <{self.from_email}>"
        mimeMessage["Date"] = formatdate(localtime=True)
        mimeMessage["Message-ID"] = f"<{uuid.uuid4()}@{sender_domain}>"
        mimeMessage["MIME-Version"] = "1.0"

        # Plain text version (must come first in multipart/alternative)
        plain_text = self._strip_html_to_plain(message)
        mimeMessage.attach(MIMEText(plain_text, "plain"))

        # HTML version
        mimeMessage.attach(MIMEText(message, "html"))

        try:
            # A transactional provider, when one is configured. Falls through to
            # the original SMTP mailbox otherwise, so nothing changes until the
            # config does.
            provider = get_provider()
            if provider is not None:
                provider.send(
                    sender=from_addr,
                    to=receiver_email,
                    subject=subject,
                    html=message,
                    text=plain_text,
                )
                print(f"✓ Email sent to {receiver_email} via {provider.name}")
                return

            # Fail with the CAUSE, not a symptom. With no SMTP host configured
            # this used to call SMTP("") and surface "[Errno -2] Name or service
            # not known" — a DNS error that reads like a network fault and sends
            # you looking in the wrong place. Unset config is not a network fault.
            missing = [
                name
                for name, value in (
                    ("EMAIL_SERVER", self.server),
                    ("EMAIL_PORT", self.port),
                    ("EMAIL_USERNAME", self.email),
                    ("EMAIL_PASSWORD", self.password),
                )
                if not value
            ]
            if missing:
                raise RuntimeError(
                    "email is not configured: " + ", ".join(missing) + " unset. "
                    "No provider is configured either, so there is no way to send. "
                    "The invite itself is still valid — copy its link from the UI."
                )

            session = SMTP(self.server, self.port)
            session.connect(self.server, self.port)
            session.starttls()
            session.ehlo()
            session.login(self.email, self.password)
            session.sendmail(self.from_email, receiver_email, mimeMessage.as_string())
            session.quit()
            print(f"✓ Email sent successfully to {receiver_email}")
        except Exception as e:
            print(f"✗ FATAL ERROR sending email to {receiver_email}: {e}")
            import traceback

            traceback.print_exc()
            raise

    def send_register_email(self, user: User):
        print(f"[EMAIL] Starting send_register_email for user: {user.username} ({user.email})")
        print(f"[EMAIL] SMTP Config - Server: {self.server}, Port: {self.port}, User: {self.email}")
        token = self.serializer.dumps(user.email, salt="email-confirmation")

        button_url = urljoin(settings.base_url_backend, f"/auth/confirm-user/{token}")
        title = "Verify your email address"

        # render and send email using the activation template
        with open(self.activation_template_path) as f:
            file_content = f.read()
            template = self.jinja.from_string(file_content)
            html = template.render(
                button_url=button_url,
            )
            self.send_email(user.email, user.username, title, html)

    def send_confirmed_email(self, user: User):
        print(f"[EMAIL] Starting send_confirmed_email for user: {user.username} ({user.email})")
        message = f"""Dear {user.username},<br><br>Your email has been verified and your Verax account is now active.<br><br>You can start exploring by choosing a plan that works for you."""
        button = "Choose a Plan"
        button_url = urljoin(settings.base_url_frontend, "/pricing")
        title = "Welcome to Verax"

        # render and send email
        with open(self.template_path) as f:
            file_content = f.read()
            template = self.jinja.from_string(file_content)
            html = template.render(
                title=title,
                message=message,
                button=button,
                button_url=button_url,
                base_url=settings.base_url_frontend,
                            )
            self.send_email(user.email, user.username, title, html)

    def send_reset_password_email(self, user: User):
        print(f"[EMAIL] Starting send_reset_password_email for user: {user.username} ({user.email})")
        print(f"[EMAIL] SMTP Config - Server: {self.server}, Port: {self.port}, User: {self.email}")
        token = self.serializer.dumps(user.email, salt="auth")

        message = f"Dear {user.username},<br><br>You have requested a password reset. Click the button below to set a new password."
        button = "Reset Password"
        button_url = urljoin(
            settings.base_url_frontend, f"/resetPassword?token={token}"
        )
        title = "Reset your password"

        # render and send email
        with open(self.template_path) as f:
            file_content = f.read()
            template = self.jinja.from_string(file_content)
            html = template.render(
                title=title,
                message=message,
                button=button,
                button_url=button_url,
                base_url=settings.base_url_frontend,
                            )
            self.send_email(user.email, user.username, title, html)

    # Month names for the expiry date. strftime("%B") follows the server's C
    # locale, which is English on every host we deploy to, so a Spanish email
    # would read "expires on August 31" — the one line that has to be
    # unambiguous, because after that date the link stops working.
    _MONTHS = {
        "en": ["January", "February", "March", "April", "May", "June", "July",
               "August", "September", "October", "November", "December"],
        "es": ["enero", "febrero", "marzo", "abril", "mayo", "junio", "julio",
               "agosto", "septiembre", "octubre", "noviembre", "diciembre"],
    }

    def send_portal_invite_email(
        self,
        recipient_email: str,
        writer_name: str,
        accept_url: str,
        expires_at=None,
        invited_by: str = None,
        language: str = "en",
    ):
        """Invite one email address to a writer's royalty portal.

        Deliberately plain: writers get this cold, and a link to sign in and
        view money reads as phishing if it is dressed up. It names the sender,
        the catalog it is for, and when it stops working.

        Written in the recipient's language. Roughly a third of this roster is
        Spanish-speaking, and an English-only email asking someone to click
        through to their earnings is the kind of thing people correctly delete.
        Unknown or unsupported codes fall back to English rather than failing —
        an invite in the wrong language still beats no invite.
        """
        lang = (language or "en").lower()[:2]
        if lang not in ("en", "es"):
            lang = "en"

        sender = invited_by or self.from_name

        expiry = ""
        if expires_at is not None:
            month = self._MONTHS[lang][expires_at.month - 1]
            when = (
                f"{month} {expires_at.day}, {expires_at.year}"
                if lang == "en"
                else f"{expires_at.day} de {month} de {expires_at.year}"
            )
            expiry = (
                f"<br><br>This link expires on {when} and can only be used once."
                if lang == "en"
                else f"<br><br>Este enlace vence el {when} y solo puede usarse una vez."
            )

        if lang == "es":
            title = f"Su portal de regalías para {writer_name} — {sender}"
            message = (
                f"{sender} le invita a acceder al portal de regalías de "
                f"<b>{writer_name}</b>.<br><br>"
                f"Allí puede consultar sus estados de cuenta, los ingresos por "
                f"canción y territorio, y descargar los documentos "
                f"correspondientes.{expiry}<br><br>"
                f"Si no esperaba este mensaje, puede ignorarlo."
            )
            button = "Abrir su portal"
        else:
            title = f"Your royalty portal for {writer_name} — {sender}"
            message = (
                f"{sender} has invited you to access the royalty portal for "
                f"<b>{writer_name}</b>.<br><br>"
                f"There you can see your statements, earnings by song and "
                f"territory, and download the underlying documents.{expiry}<br><br>"
                f"If you were not expecting this email, you can ignore it."
            )
            button = "Open your portal"

        with open(self.template_path) as f:
            template = self.jinja.from_string(f.read())
            # Brand the mail as the PUBLISHER, not as the software. The
            # recipient has a relationship with Regalias Digitales, not with
            # Verax; a VERAX wordmark over "your Verax Team" on a mail about
            # their royalties is the thing that gets it reported as phishing.
            year = expires_at.year if expires_at is not None else datetime.now().year
            html = template.render(
                title=title,
                message=message,
                button=button,
                button_url=accept_url,
                base_url=settings.base_url_frontend,
                brand_name=sender.upper(),
                # No vendor social link on a publisher's mail. Other templates
                # that omit this keep the existing default.
                social_url="",
                signoff="Kind regards," if lang == "en" else "Atentamente,",
                signoff_name=(
                    f"the {sender} team" if lang == "en" else f"el equipo de {sender}"
                ),
                footer_text=(
                    f"Copyright &copy; {year} {sender}. All rights reserved."
                    if lang == "en"
                    else f"Copyright &copy; {year} {sender}. Todos los derechos reservados."
                ),
            )
            # The recipient has no account yet, so there is no username to
            # address them by — the mailbox itself is the name we know.
            self.send_email(recipient_email, recipient_email, title, html)

    def send_claim_email(self, user: User, artist, title, message):
        message = f"""The user {user.username} has sent us a claim request.<br><br>
        Artist: {artist}<br><br>
        Title: {title}<br><br>
        Message: {message}
        """
        title = f"[CLAIM REQUEST] User {user.username} wants to claim a song"

        # render and send email
        with open(self.template_path) as f:
            file_content = f.read()
            template = self.jinja.from_string(file_content)
            html = template.render(
                title=title,
                message=message,
                base_url=settings.base_url_frontend,
                            )

            # send email to ourselves to receive customers message
            self.send_email(settings.contact_email, user.username, title, html)


def get_email_client():
    return EMail()
