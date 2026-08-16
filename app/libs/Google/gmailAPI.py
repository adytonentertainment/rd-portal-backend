# Code from https://www.youtube.com/watch?v=44ERDGa9Dr4
import os
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
import base64
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
import os.path as path
from functools import lru_cache


class GMailAPI:

    API_NAME = "gmail"
    API_VERSION = "v1"
    SCOPES = ["https://mail.google.com/"]
    CLIENT_SECRET_FILE = path.join(
        path.dirname(__file__), "..", "secrets", "client_secret.json"
    )

    def __init__(self, client_secret_file=CLIENT_SECRET_FILE):
        self.service = self.create_service(client_secret_file)

    def create_service(self, client_secret_file):

        creds = None

        if os.path.exists(client_secret_file):
            creds = Credentials.from_authorized_user_file(
                client_secret_file, self.SCOPES
            )

        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(Request())
            else:
                flow = InstalledAppFlow.from_client_secrets_file(
                    client_secret_file, self.SCOPES
                )
                creds = flow.run_local_server(port=0)

            with open(client_secret_file, "w") as token:
                token.write(creds.to_json())

        try:
            service = build(
                self.API_NAME,
                self.API_VERSION,
                credentials=creds,
                static_discovery=False,
            )
            # print(API_SERVICE_NAME, API_VERSION, 'service created successfully')
            return service
        except Exception as e:
            print(e)
            print(f"Failed to create service instance for {self.API_NAME}")
            return None

    def send_email(self, receiver_email, receiver_name, subject, message):
        emailmsg = message
        mimeMessage = MIMEMultipart("alternative")
        mimeMessage["to"] = f"{receiver_name} <{receiver_email}>"
        mimeMessage["subject"] = subject
        mimeMessage["from"] = f"TuneScan <contact@tunescan.app>"
        mimeMessage.attach(MIMEText(emailmsg, "html"))
        raw_string = base64.urlsafe_b64encode(mimeMessage.as_bytes()).decode()
        message = (
            self.service.users()
            .messages()
            .send(userId="me", body={"raw": raw_string})
            .execute()
        )


@lru_cache
def get_gmail_api():
    return GMailAPI()
