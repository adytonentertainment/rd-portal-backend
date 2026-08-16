import json
import urllib.request
import urllib.error
import base64
from typing import Optional


class DocuSealService:
    """Service for DocuSeal eSignature API integration."""

    BASE_URL = "https://api.docuseal.com"

    def __init__(self, api_token: str):
        self.api_token = api_token

    def _request(self, method: str, path: str, body: Optional[dict] = None):
        """Make authenticated REST API call to DocuSeal."""
        url = f"{self.BASE_URL}{path}"
        headers = {
            "X-Auth-Token": self.api_token,
            "Content-Type": "application/json",
        }
        data = json.dumps(body).encode("utf-8") if body else None
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            resp = urllib.request.urlopen(req)
            return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            error_body = e.read().decode("utf-8")
            raise Exception(
                f"DocuSeal API error ({e.code}): {e.reason}\n{error_body}"
            )

    def create_template_from_pdf(
        self, pdf_bytes: bytes, name: str = "Publishing Administration Agreement"
    ) -> dict:
        """Upload a PDF and create a template with signature/date fields on the last page."""
        doc_b64 = base64.b64encode(pdf_bytes).decode("utf-8")

        # Count pages to place fields on the last page
        last_page = self._count_pdf_pages(pdf_bytes)

        body = {
            "name": name,
            "documents": [
                {
                    "name": "agreement.pdf",
                    "file": f"data:application/pdf;base64,{doc_b64}",
                    "fields": [
                        # Writer signature — last page, over "Signature: /sn1/ ___"
                        {
                            "name": "Writer Signature",
                            "type": "signature",
                            "role": "First Party",
                            "required": True,
                            "areas": [
                                {
                                    "page": last_page,
                                    "x": 0.15,
                                    "y": 0.035,
                                    "w": 0.35,
                                    "h": 0.035,
                                }
                            ],
                        },
                        # Writer date — last page, over "Date: /ds1/ ___"
                        {
                            "name": "Writer Date",
                            "type": "date",
                            "role": "First Party",
                            "required": True,
                            "areas": [
                                {
                                    "page": last_page,
                                    "x": 0.08,
                                    "y": 0.073,
                                    "w": 0.18,
                                    "h": 0.025,
                                }
                            ],
                        },
                        # Admin signature — last page, over "Signature: /sn2/ ___"
                        {
                            "name": "Admin Signature",
                            "type": "signature",
                            "role": "Second Party",
                            "required": True,
                            "areas": [
                                {
                                    "page": last_page,
                                    "x": 0.15,
                                    "y": 0.185,
                                    "w": 0.35,
                                    "h": 0.035,
                                }
                            ],
                        },
                        # Admin date — last page, over "Date: /ds2/ ___"
                        {
                            "name": "Admin Date",
                            "type": "date",
                            "role": "Second Party",
                            "required": True,
                            "areas": [
                                {
                                    "page": last_page,
                                    "x": 0.08,
                                    "y": 0.222,
                                    "w": 0.18,
                                    "h": 0.025,
                                }
                            ],
                        },
                    ],
                }
            ],
        }
        return self._request("POST", "/templates/pdf", body)

    def _count_pdf_pages(self, pdf_bytes: bytes) -> int:
        """Count pages in a PDF. Falls back to 1 if parsing fails."""
        try:
            # Simple PDF page count by searching for /Type /Page entries
            content = pdf_bytes.decode("latin-1")
            # Count /Type /Page (but not /Type /Pages)
            import re
            pages = re.findall(r"/Type\s*/Page(?!\s*s)", content)
            return max(len(pages), 1)
        except Exception:
            return 1

    def create_submission(
        self,
        template_id: int,
        signer_name: str,
        signer_email: str,
        completed_redirect_url: str,
        send_email: bool = False,
        values: Optional[dict] = None,
        admin_name: Optional[str] = None,
        admin_email: Optional[str] = None,
    ) -> list:
        """Create a submission (signature request). Returns list of submitters with slugs."""
        first_party = {
            "name": signer_name,
            "email": signer_email,
            "role": "First Party",
            "send_email": send_email,
            "order": 0,
        }
        if values:
            first_party["values"] = values

        submitters = [first_party]

        # Add admin/company counter-signer if provided
        if admin_name and admin_email:
            submitters.append({
                "name": admin_name,
                "email": admin_email,
                "role": "Second Party",
                "send_email": True,
                "order": 1,
            })

        body = {
            "template_id": template_id,
            "send_email": send_email,
            "order": "preserved",
            "completed_redirect_url": completed_redirect_url,
            "submitters": submitters,
        }
        return self._request("POST", "/submissions", body)

    def get_submission(self, submission_id: int) -> dict:
        """Get submission status and details."""
        return self._request("GET", f"/submissions/{submission_id}")

    def get_submitter_signing_url(self, slug: str) -> str:
        """Build the embedded signing URL from a submitter slug."""
        return f"https://docuseal.com/s/{slug}"
