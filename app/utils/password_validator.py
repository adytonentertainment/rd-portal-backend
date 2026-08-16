"""
Password validation utilities for strong password requirements.

Requirements:
- Minimum 8 characters
- At least 1 uppercase letter
- At least 1 lowercase letter
- At least 1 number
- At least 1 special character
"""

import re
from typing import List, Tuple

# Password requirements
MIN_LENGTH = 8
SPECIAL_CHARS = r"!@#$%^&*()_+\-=\[\]{};':\"\\|,.<>\/?~`"

# Regex patterns
HAS_UPPERCASE = re.compile(r"[A-Z]")
HAS_LOWERCASE = re.compile(r"[a-z]")
HAS_DIGIT = re.compile(r"[0-9]")
HAS_SPECIAL = re.compile(rf"[{re.escape(SPECIAL_CHARS)}]")


def validate_password(password: str) -> Tuple[bool, List[str]]:
    """
    Validate password against strong password requirements.

    Args:
        password: The password to validate

    Returns:
        Tuple of (is_valid, list_of_error_messages)
    """
    errors = []

    if len(password) < MIN_LENGTH:
        errors.append(f"Password must be at least {MIN_LENGTH} characters long")

    if not HAS_UPPERCASE.search(password):
        errors.append("Password must contain at least one uppercase letter")

    if not HAS_LOWERCASE.search(password):
        errors.append("Password must contain at least one lowercase letter")

    if not HAS_DIGIT.search(password):
        errors.append("Password must contain at least one number")

    if not HAS_SPECIAL.search(password):
        errors.append(
            "Password must contain at least one special character (!@#$%^&*()_+-=[]{}|;':\",./<>?~`)"
        )

    return (len(errors) == 0, errors)


def get_password_requirements() -> str:
    """
    Get a human-readable description of password requirements.

    Returns:
        String describing password requirements
    """
    return (
        f"Password must be at least {MIN_LENGTH} characters long and contain "
        "at least one uppercase letter, one lowercase letter, one number, "
        "and one special character (!@#$%^&*()_+-=[]{}|;':\",./<>?~`)"
    )
