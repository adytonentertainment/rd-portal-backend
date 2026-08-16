from datetime import datetime, timedelta
from collections import defaultdict
from typing import Dict, Tuple
from fastapi import HTTPException, status


class AccountLockout:
    """
    Tracks failed login attempts and locks accounts after threshold is reached.
    Prevents brute force attacks on user accounts.
    """

    def __init__(
        self,
        max_attempts: int = 5,
        lockout_duration_minutes: int = 30,
        reset_window_minutes: int = 15,
    ):
        """
        Args:
            max_attempts: Maximum failed attempts before lockout
            lockout_duration_minutes: How long to lock the account
            reset_window_minutes: Time window to reset failed attempts counter
        """
        self.max_attempts = max_attempts
        self.lockout_duration = timedelta(minutes=lockout_duration_minutes)
        self.reset_window = timedelta(minutes=reset_window_minutes)

        # Store: {identifier: (failed_attempts, first_attempt_time, lockout_time)}
        self.attempts: Dict[str, Tuple[int, datetime, datetime]] = defaultdict(
            lambda: (0, datetime.now(), None)
        )

    def check_lockout(self, identifier: str) -> None:
        """
        Check if an account is locked out.

        Args:
            identifier: Username or email to check

        Raises:
            HTTPException: If account is currently locked
        """
        if identifier not in self.attempts:
            return

        failed_count, first_attempt, lockout_time = self.attempts[identifier]

        # Check if account is locked
        if lockout_time and datetime.now() < lockout_time:
            remaining = (lockout_time - datetime.now()).seconds // 60
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail=f"Account locked due to too many failed login attempts. Try again in {remaining} minutes.",
            )

        # Reset lockout if duration has passed
        if lockout_time and datetime.now() >= lockout_time:
            del self.attempts[identifier]

    def record_failed_attempt(self, identifier: str) -> None:
        """
        Record a failed login attempt.

        Args:
            identifier: Username or email that failed
        """
        now = datetime.now()

        if identifier in self.attempts:
            failed_count, first_attempt, lockout_time = self.attempts[identifier]

            # Reset counter if outside reset window
            if now - first_attempt > self.reset_window:
                failed_count = 0
                first_attempt = now

            failed_count += 1

            # Lock account if threshold reached
            if failed_count >= self.max_attempts:
                lockout_time = now + self.lockout_duration
                self.attempts[identifier] = (failed_count, first_attempt, lockout_time)
            else:
                self.attempts[identifier] = (failed_count, first_attempt, lockout_time)
        else:
            # First failed attempt
            self.attempts[identifier] = (1, now, None)

    def record_successful_login(self, identifier: str) -> None:
        """
        Clear failed attempts after successful login.

        Args:
            identifier: Username or email that logged in successfully
        """
        if identifier in self.attempts:
            del self.attempts[identifier]

    def get_remaining_attempts(self, identifier: str) -> int:
        """
        Get remaining login attempts before lockout.

        Args:
            identifier: Username or email to check

        Returns:
            Number of attempts remaining
        """
        if identifier not in self.attempts:
            return self.max_attempts

        failed_count, first_attempt, lockout_time = self.attempts[identifier]

        # If locked, no attempts remaining
        if lockout_time and datetime.now() < lockout_time:
            return 0

        # If outside reset window, full attempts available
        if datetime.now() - first_attempt > self.reset_window:
            return self.max_attempts

        return max(0, self.max_attempts - failed_count)


# Global account lockout tracker
account_lockout = AccountLockout(
    max_attempts=5, lockout_duration_minutes=30, reset_window_minutes=15
)
