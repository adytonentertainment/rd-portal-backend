import base64
import hashlib
import hmac
import json
import urllib.request
import urllib.error
from datetime import datetime, timedelta

from docusign_esign import ApiClient

from app.settings.settings import get_settings


class DocuSignService:
    """Service for DocuSign eSignature API integration using JWT Grant auth."""

    def __init__(self):
        self.settings = get_settings()
        self._access_token = None
        self._token_expiry = None
        self._base_url = self.settings.docusign_base_url.rstrip("/")
        self._account_id = self.settings.docusign_account_id

    def _ensure_valid_token(self):
        """Obtain or refresh the JWT access token."""
        if self._access_token and self._token_expiry and datetime.now() < self._token_expiry:
            return

        key_path = self.settings.docusign_rsa_private_key_path
        with open(key_path, "r") as f:
            private_key = f.read().encode("utf-8")

        auth_client = ApiClient()
        auth_client.set_base_path(f"https://{self.settings.docusign_auth_server}")
        token_response = auth_client.request_jwt_user_token(
            client_id=self.settings.docusign_integration_key,
            user_id=self.settings.docusign_user_id,
            oauth_host_name=self.settings.docusign_auth_server,
            private_key_bytes=private_key,
            expires_in=3600,
            scopes=["signature", "impersonation"],
        )

        self._access_token = token_response.access_token
        self._token_expiry = datetime.now() + timedelta(seconds=3500)

    def _api_request(self, method, path, body=None):
        """Make authenticated REST API call to DocuSign."""
        url = f"{self._base_url}/v2.1/accounts/{self._account_id}{path}"
        headers = {
            "Authorization": f"Bearer {self._access_token}",
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
                f"({e.code})\n"
                f"Reason: {e.reason}\n"
                f"HTTP response body: {error_body}"
            )

    def _api_request_raw(self, method, path):
        """Make authenticated REST API call returning raw bytes."""
        url = f"{self._base_url}/v2.1/accounts/{self._account_id}{path}"
        headers = {
            "Authorization": f"Bearer {self._access_token}",
        }
        req = urllib.request.Request(url, headers=headers, method=method)
        resp = urllib.request.urlopen(req)
        return resp.read()

    def create_envelope_with_embedded_signing(
        self,
        pdf_bytes: bytes,
        signer_name: str,
        signer_email: str,
        client_user_id: str,
        return_url: str,
    ) -> tuple:
        """Create a DocuSign envelope and return (envelope_id, signing_url)."""
        self._ensure_valid_token()

        doc_b64 = base64.b64encode(pdf_bytes).decode("utf-8")

        envelope_body = {
            "emailSubject": "Publishing Administration Agreement - Signature Required",
            "documents": [
                {
                    "documentBase64": doc_b64,
                    "name": "Publishing Administration Agreement",
                    "fileExtension": "pdf",
                    "documentId": "1",
                }
            ],
            "recipients": {
                "signers": [
                    {
                        "email": signer_email,
                        "name": signer_name,
                        "recipientId": "1",
                        "routingOrder": "1",
                        "clientUserId": client_user_id,
                        "tabs": {
                            "signHereTabs": [
                                {
                                    "anchorString": "/sn1/",
                                    "anchorUnits": "pixels",
                                    "anchorXOffset": "10",
                                    "anchorYOffset": "-5",
                                }
                            ],
                            "dateSignedTabs": [
                                {
                                    "anchorString": "/ds1/",
                                    "anchorUnits": "pixels",
                                    "anchorXOffset": "10",
                                    "anchorYOffset": "-5",
                                }
                            ],
                        },
                    },
                    {
                        "email": self.settings.docusign_admin_signer_email,
                        "name": self.settings.docusign_admin_signer_name,
                        "recipientId": "2",
                        "routingOrder": "2",
                        "tabs": {
                            "signHereTabs": [
                                {
                                    "anchorString": "/sn2/",
                                    "anchorUnits": "pixels",
                                    "anchorXOffset": "10",
                                    "anchorYOffset": "-5",
                                }
                            ],
                            "dateSignedTabs": [
                                {
                                    "anchorString": "/ds2/",
                                    "anchorUnits": "pixels",
                                    "anchorXOffset": "10",
                                    "anchorYOffset": "-5",
                                }
                            ],
                        },
                    },
                ]
            },
            "status": "sent",
        }

        result = self._api_request("POST", "/envelopes", envelope_body)
        envelope_id = result["envelopeId"]

        # Create embedded signing URL for the writer
        view_body = {
            "authenticationMethod": "none",
            "clientUserId": client_user_id,
            "recipientId": "1",
            "returnUrl": return_url,
            "userName": signer_name,
            "email": signer_email,
        }
        view_result = self._api_request(
            "POST", f"/envelopes/{envelope_id}/views/recipient", view_body
        )

        return envelope_id, view_result["url"]

    def get_envelope_status(self, envelope_id: str) -> str:
        """Query DocuSign for current envelope status."""
        self._ensure_valid_token()
        result = self._api_request("GET", f"/envelopes/{envelope_id}")
        return result["status"]

    def download_signed_document(self, envelope_id: str) -> bytes:
        """Download the completed/signed PDF from DocuSign."""
        self._ensure_valid_token()
        return self._api_request_raw(
            "GET", f"/envelopes/{envelope_id}/documents/1"
        )

    @staticmethod
    def verify_webhook_signature(
        payload: bytes, signature: str, hmac_secret: str
    ) -> bool:
        """Verify DocuSign Connect webhook HMAC-SHA256 signature."""
        if not signature or not hmac_secret:
            return False
        computed = base64.b64encode(
            hmac.new(
                hmac_secret.encode("utf-8"),
                payload,
                hashlib.sha256,
            ).digest()
        ).decode("utf-8")
        return hmac.compare_digest(computed, signature)
