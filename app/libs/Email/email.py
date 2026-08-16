from email import encoders
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from smtplib import SMTP
import os


class EMail:
    # uses the same api as the gmail service
    def __init__(self):
        # connect to smtp server
        self.email = os.environ.get("EMAIL_USERNAME")
        self.server = os.environ.get("EMAIL_SERVER")
        self.port = os.environ.get("EMAIL_PORT")
        self.password = os.environ.get("EMAIL_PASSWORD")

        self.smtp = SMTP(self.server, self.port)
        self.smtp.connect(self.server, self.port)
        self.smtp.starttls()
        self.smtp.ehlo()
        self.smtp.login(self.email, self.password)

    def send_email(self, receiver_email, receiver_name, subject, message):

        to_addr = f"{receiver_name} <{receiver_email}>"
        from_addr = f"TuneScan <{self.email}>"

        emailmsg = message
        mimeMessage = MIMEMultipart("alternative")
        mimeMessage["to"] = to_addr
        mimeMessage["subject"] = subject
        mimeMessage["from"] = from_addr
        mimeMessage.attach(MIMEText(emailmsg, "html"))

        self.smtp.sendmail(from_addr, to_addr, mimeMessage.as_string())

    def send_email_with_attachment(
        self,
        receiver_email,
        receiver_name,
        subject,
        message,
        attachment_bytes,
        attachment_filename,
    ):
        to_addr = f"{receiver_name} <{receiver_email}>"
        from_addr = f"Verax <{self.email}>"

        mimeMessage = MIMEMultipart("mixed")
        mimeMessage["to"] = to_addr
        mimeMessage["subject"] = subject
        mimeMessage["from"] = from_addr

        mimeMessage.attach(MIMEText(message, "html"))

        attachment = MIMEBase("application", "pdf")
        attachment.set_payload(attachment_bytes)
        encoders.encode_base64(attachment)
        attachment.add_header(
            "Content-Disposition",
            f"attachment; filename={attachment_filename}",
        )
        mimeMessage.attach(attachment)

        self.smtp.sendmail(from_addr, to_addr, mimeMessage.as_string())

    def send_signed_agreement_email(
        self, receiver_email, receiver_name, pdf_bytes, signer_name
    ):
        subject = "Your Signed Publishing Administration Agreement - Verax"
        message = f"""
        <html>
        <body style="font-family: sans-serif; color: #333;">
            <h2>Publishing Administration Agreement</h2>
            <p>Hello {receiver_name},</p>
            <p>The Publishing Administration Agreement with <b>{signer_name}</b>
            has been fully executed by all parties.</p>
            <p>Please find the signed agreement attached to this email as a PDF.</p>
            <br>
            <p>Best regards,<br>Verax / Adyton Entertainment LLC</p>
        </body>
        </html>
        """
        self.send_email_with_attachment(
            receiver_email=receiver_email,
            receiver_name=receiver_name,
            subject=subject,
            message=message,
            attachment_bytes=pdf_bytes,
            attachment_filename="Publishing_Administration_Agreement_Signed.pdf",
        )
