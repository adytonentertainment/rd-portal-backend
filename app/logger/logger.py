import logging
import re
import sys
import os
from datetime import datetime
from logging.handlers import TimedRotatingFileHandler

from app.settings import get_settings

settings = get_settings()

# Anything that looks like a JWT (three base64url segments after the standard
# "eyJ" header prefix) or a bearer credential. Session tokens ARE credentials:
# whoever holds one is that user until it expires, without a password, and a
# password change does not revoke it.
_SECRET_PATTERNS = [
    re.compile(r"eyJ[A-Za-z0-9_-]{4,}\.[A-Za-z0-9_-]{4,}\.[A-Za-z0-9_-]{4,}"),
    re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]{12,}"),
    re.compile(r"(?i)([?&](?:token|access_token|api_key|password)=)[^&\s\"']+"),
]


def _redact(text: str) -> str:
    text = _SECRET_PATTERNS[0].sub("[REDACTED-JWT]", text)
    text = _SECRET_PATTERNS[1].sub(r"\1[REDACTED]", text)
    text = _SECRET_PATTERNS[2].sub(r"\1[REDACTED]", text)
    return text


class RedactSecretsFilter(logging.Filter):
    """Strip credentials from every log record.

    A session token reached the logs because one endpoint carried it in the URL
    path and the access logger writes request lines verbatim. Rather than trust
    that no future endpoint ever does that again, redact at the logging layer:
    any token that reaches a handler is scrubbed before it hits disk.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            if isinstance(record.msg, str) and "eyJ" in record.msg or "earer" in str(record.msg):
                record.msg = _redact(str(record.msg))
            if record.args:
                if isinstance(record.args, dict):
                    record.args = {k: _redact(v) if isinstance(v, str) else v
                                   for k, v in record.args.items()}
                else:
                    record.args = tuple(
                        _redact(a) if isinstance(a, str) else a for a in record.args
                    )
        except Exception:  # logging must never break the request
            pass
        return True


_redact_filter = RedactSecretsFilter()

# Global file handler to be shared across all loggers
_file_handler = None
_logs_dir = None


def _get_file_handler():
    """Get or create the shared file handler for daily log rotation."""
    global _file_handler, _logs_dir

    if _file_handler is not None:
        return _file_handler

    # Create logs directory if it doesn't exist
    env_name = settings.mode if settings.mode else "development"
    _logs_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "logs")
    os.makedirs(_logs_dir, exist_ok=True)

    # Use base filename without date - TimedRotatingFileHandler will add date suffix on rotation
    # Current log is always {env_name}.log, rotated logs become {env_name}.log.2025-12-10
    log_file_path = os.path.join(_logs_dir, f"{env_name}.log")

    # Use TimedRotatingFileHandler for automatic daily rotation at midnight
    _file_handler = TimedRotatingFileHandler(
        filename=log_file_path,
        when="midnight",
        interval=1,
        backupCount=30,  # Keep 30 days of logs
        encoding="utf-8"
    )
    # Rotated files will be named: {env_name}.log.2025-12-10
    _file_handler.suffix = "%Y-%m-%d"

    log_formatter = logging.Formatter(
        fmt="%(asctime)s [%(levelname)s]: %(message)s",
        datefmt="[%d.%m.%Y] [%H:%M:%S]",
    )
    _file_handler.setFormatter(log_formatter)
    _file_handler.addFilter(_redact_filter)

    return _file_handler


def setup_logging():
    """Configure logging for the entire application including uvicorn."""
    file_handler = _get_file_handler()

    # Configure root logger to capture all logs
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO)
    root_logger.addFilter(_redact_filter)

    # Add file handler to root logger if not already added
    if file_handler not in root_logger.handlers:
        root_logger.addHandler(file_handler)

    # Configure uvicorn loggers to use our handler
    for logger_name in ["uvicorn", "uvicorn.error", "uvicorn.access"]:
        uvicorn_logger = logging.getLogger(logger_name)
        uvicorn_logger.handlers = []  # Remove default handlers
        uvicorn_logger.addHandler(file_handler)
        uvicorn_logger.addFilter(_redact_filter)
        uvicorn_logger.propagate = False


def get_logger(name: str = None):
    """Get a logger instance with console and file output."""
    logger_name = name if name else __name__
    logger = logging.getLogger(logger_name)
    logger.setLevel(logging.DEBUG)

    # Console handler for stdout
    stream_handler = logging.StreamHandler(sys.stdout)
    log_formatter = logging.Formatter(
        fmt=f"%(asctime)s {f'[{name.upper()}] ' if name else ''}[%(levelname)s]: %(message)s",
        datefmt="[%d.%m.%Y] [%H:%M:%S]",
    )
    stream_handler.setFormatter(log_formatter)
    stream_handler.addFilter(_redact_filter)

    # File handler (shared)
    file_handler = _get_file_handler()

    if not logger.hasHandlers():
        logger.addHandler(stream_handler)
        logger.addHandler(file_handler)

    logger.propagate = False
    return logger
