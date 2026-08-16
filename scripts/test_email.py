"""Prove the SMTP settings work before trusting them with a client invite.

    ENVIRONMENT=DEVELOPMENT python scripts/test_email.py you@yourdomain.com

Sends one real invite-shaped email and reports exactly what the server said.
Run this after changing EMAIL_* config — a 535 here is a credentials problem,
not an application problem.
"""

import socket
import sys

socket.setdefaulttimeout(30)

from app.emails.emails import EMail  # noqa: E402
from app.settings import get_settings  # noqa: E402


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    to = sys.argv[1]
    s = get_settings()
    sender = s.email_from or s.email_username
    print(f"SMTP server : {s.email_server}:{s.email_port}")
    print(f"login as    : {s.email_username}")
    print(f"sending as  : {s.email_from_name} <{sender}>")
    print(f"to          : {to}\n")
    try:
        EMail().send_portal_invite_email(
            recipient_email=to,
            writer_name="Test Client (delivery check)",
            accept_url="https://example.invalid/invite/not-a-real-token",
        )
    except Exception as exc:  # noqa: BLE001 — the reason is the whole point
        print(f"FAILED: {type(exc).__name__}: {exc}")
        print("\n535 / 'credentials invalid' -> wrong username or password.")
        print("550 / 'not permitted'        -> EMAIL_FROM is not an address this")
        print("                                mailbox is allowed to send as.")
        return 1
    print("OK — the server accepted the message. Check the inbox AND the spam folder.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
