import json
import os
import uuid
from datetime import datetime

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.database.session import get_session
from app.models.models import DocuSignPublishingAgreement, DocuSignStatus
from app.schemas.publishing_admin import (
    PublishingAdminInitiateRequest,
    PublishingAdminInitiateResponse,
    PublishingAdminStatusResponse,
)
from app.services.docuseal_service import DocuSealService
from app.services.publishing_admin_pdf import PublishingAdminPDFGenerator
from app.settings.settings import get_settings
from app.logger.logger import get_logger

logger = get_logger("publishing_admin")

settings = get_settings()

publishing_admin_router = APIRouter(
    prefix="/publishing-admin",
    tags=["Publishing Admin"],
)

AGREEMENT_TYPES = {
    "3month": "30-Day Rolling Administration",
    "2year": "2-Year Administration",
}

STORAGE_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
    "storage",
    "publishing-agreements",
)


def _ensure_storage_dir():
    os.makedirs(STORAGE_DIR, exist_ok=True)


@publishing_admin_router.post(
    "/initiate", response_model=PublishingAdminInitiateResponse
)
async def initiate_signing(
    request: PublishingAdminInitiateRequest,
    db: Session = Depends(get_session),
):
    """Generate PDF, create DocuSeal submission, return embedded signing URL."""

    # Validate DocuSeal is configured
    if not settings.docuseal_api_token:
        raise HTTPException(
            status_code=503,
            detail="DocuSeal is not configured on this server.",
        )

    form_data = {
        "legalName": request.legalName,
        "producerName": request.producerName,
        "email": request.email,
        "address": request.address,
        "city": request.city,
        "state": request.state,
        "zip": request.zip,
        "country": request.country,
        "termType": request.termType,
    }

    # Generate PDF
    pdf_gen = PublishingAdminPDFGenerator()
    pdf_buffer = pdf_gen.generate(form_data)
    pdf_bytes = pdf_buffer.read()

    # Save unsigned PDF
    _ensure_storage_dir()
    pdf_filename = f"unsigned_{uuid.uuid4().hex[:12]}.pdf"
    pdf_path = os.path.join(STORAGE_DIR, pdf_filename)
    with open(pdf_path, "wb") as f:
        f.write(pdf_bytes)

    # Create DB record
    agreement = DocuSignPublishingAgreement(
        signer_legal_name=request.legalName,
        signer_producer_name=request.producerName,
        signer_email=request.email,
        signer_address=request.address,
        signer_city=request.city,
        signer_state=request.state,
        signer_zip=request.zip,
        signer_country=request.country,
        term_type=request.termType,
        unsigned_pdf_path=pdf_path,
        agreement_date=datetime.now(),
        status=DocuSignStatus.DRAFT,
    )
    db.add(agreement)
    db.commit()
    db.refresh(agreement)

    # Create DocuSeal template from the generated PDF and initiate signing
    return_url = (
        f"{settings.base_url_frontend}publishing"
        f"?event=signing_complete&agreementId={agreement.id}"
    )

    try:
        docuseal = DocuSealService(settings.docuseal_api_token)

        # Upload the PDF as a template
        template = docuseal.create_template_from_pdf(
            pdf_bytes=pdf_bytes,
            name=f"Publishing Agreement - {request.legalName}",
        )
        template_id = template["id"]

        # Create a submission (signature request) with counter-signer
        admin_name = settings.docusign_admin_signer_name or "Adyton Entertainment S.L."
        admin_email = settings.docusign_admin_signer_email or settings.contact_email

        submitters = docuseal.create_submission(
            template_id=template_id,
            signer_name=request.legalName,
            signer_email=request.email,
            completed_redirect_url=return_url,
            send_email=True,
            admin_name=admin_name,
            admin_email=admin_email,
        )

        # Find First Party and Second Party submitters from response
        logger.info(f"DocuSeal submission response: {json.dumps(submitters, default=str)}")

        first_party_sub = None
        second_party_sub = None
        for s in submitters:
            if s.get("role") == "First Party":
                first_party_sub = s
            elif s.get("role") == "Second Party":
                second_party_sub = s

        if not first_party_sub:
            # Fallback: first item is First Party
            first_party_sub = submitters[0]
            if len(submitters) > 1:
                second_party_sub = submitters[1]

        signing_url = first_party_sub.get("embed_src") or f"https://docuseal.com/s/{first_party_sub.get('slug', '')}"
        envelope_id = str(first_party_sub["id"])

        logger.info(
            f"First Party: id={first_party_sub.get('id')}, slug={first_party_sub.get('slug')}, "
            f"Second Party: {second_party_sub is not None}"
        )

    except Exception as e:
        logger.error(f"DocuSeal submission creation failed: {e}")
        raise HTTPException(
            status_code=500,
            detail=f"Failed to create signing envelope: {str(e)}",
        )

    # Update DB with envelope ID (reusing docusign_envelope_id column for DocuSeal submitter ID)
    agreement.docusign_envelope_id = envelope_id
    agreement.status = DocuSignStatus.SENT
    db.commit()

    # Send counter-sign notification email to admin with their signing link
    counter_sign_email_sent = False
    counter_sign_error = None
    if second_party_sub:
        try:
            from email.mime.multipart import MIMEMultipart
            from email.mime.text import MIMEText
            import smtplib

            admin_slug = second_party_sub.get("slug", "")
            admin_signing_url = f"https://docuseal.com/s/{admin_slug}"

            logger.info(
                f"Sending counter-sign email to {admin_email} via "
                f"{settings.email_server}:{settings.email_port}"
            )

            # Try STARTTLS first, fall back to SMTP_SSL
            try:
                smtp = smtplib.SMTP(settings.email_server, settings.email_port, timeout=30)
                smtp.starttls()
                smtp.ehlo()
            except Exception as tls_err:
                logger.warning(f"STARTTLS failed ({tls_err}), trying SMTP_SSL on 465")
                smtp = smtplib.SMTP_SSL(settings.email_server, 465, timeout=30)

            smtp.login(settings.email_username, settings.email_password)

            from_addr = f"Verax <{settings.email_username}>"
            to_addr = admin_email

            msg = MIMEMultipart("alternative")
            msg["from"] = from_addr
            msg["to"] = to_addr
            msg["subject"] = f"Counter-Sign Required: Publishing Agreement — {request.legalName}"

            html = f"""
            <html>
            <body style="font-family: sans-serif; color: #333;">
                <h2>New Publishing Agreement Awaiting Counter-Signature</h2>
                <p><b>{request.legalName}</b> (p/k/a "{request.producerName}") has been sent a
                publishing administration agreement to sign.</p>
                <p>Once they complete their signature, you can counter-sign using the link below:</p>
                <p style="margin: 1.5rem 0;">
                    <a href="{admin_signing_url}"
                       style="background: #111; color: #fff; padding: 12px 24px;
                              border-radius: 8px; text-decoration: none; font-weight: 600;">
                        Counter-Sign Agreement
                    </a>
                </p>
                <p style="font-size: 0.85rem; color: #666;">
                    Note: The link will become active after the writer completes their signature.
                </p>
                <hr style="border: none; border-top: 1px solid #eee; margin: 1.5rem 0;">
                <p style="font-size: 0.8rem; color: #999;">
                    Signer: {request.legalName} &lt;{request.email}&gt;<br>
                    Agreement: {AGREEMENT_TYPES.get(request.termType, request.termType)}<br>
                    Address: {request.address}, {request.city}, {request.state} {request.zip}, {request.country}
                </p>
            </body>
            </html>
            """
            msg.attach(MIMEText(html, "html"))
            smtp.sendmail(from_addr, to_addr, msg.as_string())
            smtp.quit()

            counter_sign_email_sent = True
            logger.info(
                f"Counter-sign email sent to {admin_email} for agreement {agreement.id}"
            )
        except Exception as e:
            import traceback
            counter_sign_error = str(e)
            logger.error(f"Failed to send counter-sign email: {e}\n{traceback.format_exc()}")
    else:
        logger.warning("No Second Party submitter found — counter-sign email not sent")

    logger.info(
        f"Publishing agreement {agreement.id} created via DocuSeal, "
        f"submitter {envelope_id} sent to {request.email}"
    )

    return PublishingAdminInitiateResponse(
        agreement_id=agreement.id,
        signing_url=signing_url,
        envelope_id=envelope_id,
    )


@publishing_admin_router.get(
    "/status/{envelope_id}", response_model=PublishingAdminStatusResponse
)
async def get_signing_status(
    envelope_id: str,
    db: Session = Depends(get_session),
):
    """Check the signing status of an agreement."""
    agreement = (
        db.query(DocuSignPublishingAgreement)
        .filter(DocuSignPublishingAgreement.docusign_envelope_id == envelope_id)
        .first()
    )
    if not agreement:
        raise HTTPException(status_code=404, detail="Agreement not found")

    return PublishingAdminStatusResponse(
        envelope_id=envelope_id,
        status=agreement.status.value,
        signer_name=agreement.signer_legal_name,
        created_at=agreement.created_at.isoformat(),
        signed_at=agreement.signed_at.isoformat() if agreement.signed_at else None,
    )


@publishing_admin_router.post("/webhook/docuseal")
async def docuseal_webhook(
    request: Request,
    db: Session = Depends(get_session),
):
    """DocuSeal webhook — handles submission status changes."""
    payload = await request.body()
    data = json.loads(payload)

    event_type = data.get("event_type", "")
    submitter_data = data.get("data", {})
    submitter_id = str(submitter_data.get("id", ""))

    if not submitter_id:
        return {"status": "ignored", "reason": "no submitter ID"}

    agreement = (
        db.query(DocuSignPublishingAgreement)
        .filter(DocuSignPublishingAgreement.docusign_envelope_id == submitter_id)
        .first()
    )
    if not agreement:
        logger.warning(f"DocuSeal webhook: unknown submitter {submitter_id}")
        return {"status": "ignored"}

    logger.info(f"DocuSeal webhook: {event_type} for submitter {submitter_id}")

    if event_type == "form.completed":
        # Check the submitter's role to determine if this is the writer or admin signing
        submitter_role = submitter_data.get("role", "")

        if submitter_role == "Second Party":
            # Admin (counter-signer) has signed — agreement is fully executed
            agreement.status = DocuSignStatus.COMPLETED
            agreement.signed_at = datetime.now()

            # Try to email the fully signed agreement to both parties
            try:
                documents = submitter_data.get("documents", [])
                if documents:
                    import urllib.request
                    signed_pdf_url = documents[0].get("url", "")
                    if signed_pdf_url:
                        req = urllib.request.Request(signed_pdf_url)
                        resp = urllib.request.urlopen(req)
                        signed_pdf_bytes = resp.read()

                        # Save fully signed PDF
                        _ensure_storage_dir()
                        signed_filename = f"signed_{agreement.id}_{uuid.uuid4().hex[:8]}.pdf"
                        signed_path = os.path.join(STORAGE_DIR, signed_filename)
                        with open(signed_path, "wb") as f:
                            f.write(signed_pdf_bytes)
                        agreement.signed_pdf_path = signed_path

                        # Email fully signed PDF to both parties
                        from app.libs.Email.email import EMail

                        email_client = EMail()
                        email_client.send_signed_agreement_email(
                            receiver_email=agreement.signer_email,
                            receiver_name=agreement.signer_legal_name,
                            pdf_bytes=signed_pdf_bytes,
                            signer_name=agreement.signer_legal_name,
                        )
                        admin_email = settings.docusign_admin_signer_email or settings.contact_email
                        email_client.send_signed_agreement_email(
                            receiver_email=admin_email,
                            receiver_name=settings.docusign_admin_signer_name or "Adyton Entertainment S.L.",
                            pdf_bytes=signed_pdf_bytes,
                            signer_name=agreement.signer_legal_name,
                        )
                        agreement.pdf_delivered_at = datetime.now()
                        logger.info(
                            f"Fully signed PDF delivered for agreement {agreement.id} "
                            f"to {agreement.signer_email} and {admin_email}"
                        )
            except Exception as e:
                logger.error(
                    f"Failed to process signed document for submitter {submitter_id}: {e}"
                )
        else:
            # Writer (First Party) has signed — waiting for admin counter-signature
            agreement.status = DocuSignStatus.SENT
            logger.info(
                f"Writer signed agreement {agreement.id}, "
                f"awaiting admin counter-signature"
            )

    elif event_type == "form.declined":
        agreement.status = DocuSignStatus.DECLINED

    db.commit()
    return {"status": "ok"}


# ─── Publishing admin inquiry (lead capture) ─────────────────────────

class PublishingInquiryRequest(BaseModel):
    firstName: str
    lastName: str
    email: str
    catalogSize: str
    geniusUrl: Optional[str] = None


@publishing_admin_router.post("/inquiry")
async def publishing_admin_inquiry(body: PublishingInquiryRequest):
    """Capture a publishing admin lead and notify via email."""
    try:
        from email.mime.multipart import MIMEMultipart
        from email.mime.text import MIMEText
        import smtplib

        try:
            smtp = smtplib.SMTP(settings.email_server, settings.email_port, timeout=30)
            smtp.starttls()
            smtp.ehlo()
        except Exception:
            smtp = smtplib.SMTP_SSL(settings.email_server, 465, timeout=30)
        smtp.login(settings.email_username, settings.email_password)

        from_addr = f"Verax <{settings.email_username}>"
        to_addr = settings.contact_email or settings.email_username

        msg = MIMEMultipart("alternative")
        msg["from"] = from_addr
        msg["to"] = to_addr
        msg["subject"] = f"Publishing Admin Inquiry — {body.firstName} {body.lastName}"

        genius_line = (
            f"<p><b>Genius:</b> <a href=\"{body.geniusUrl}\">{body.geniusUrl}</a></p>"
            if body.geniusUrl
            else ""
        )

        html = f"""
        <html>
        <body style="font-family: sans-serif; color: #333;">
            <h2>New Publishing Admin Inquiry</h2>
            <p><b>Name:</b> {body.firstName} {body.lastName}</p>
            <p><b>Email:</b> {body.email}</p>
            <p><b>Catalog Size:</b> {body.catalogSize}</p>
            {genius_line}
        </body>
        </html>
        """
        msg.attach(MIMEText(html, "html"))
        smtp.sendmail(from_addr, to_addr, msg.as_string())
        smtp.quit()

        logger.info(
            f"[PublishingAdmin] Inquiry from {body.firstName} {body.lastName} "
            f"({body.email}), catalog: {body.catalogSize}"
        )
        return {"success": True}

    except Exception as e:
        import traceback
        logger.error(f"[PublishingAdmin] Inquiry failed: {e}\n{traceback.format_exc()}")
        raise HTTPException(status_code=500, detail=f"Failed to submit inquiry: {str(e)}")
