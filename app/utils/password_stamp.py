"""Fingerprint of the current password hash.

Embedded in session tokens and password-reset links. Changing the password
changes the stamp, so a token issued before the change no longer matches.
"""

import hashlib
from typing import Optional


def password_stamp(hashed_password: Optional[str]) -> str:
    return hashlib.sha256((hashed_password or "").encode()).hexdigest()[:16]
