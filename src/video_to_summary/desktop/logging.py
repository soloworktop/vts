"""Persistent logs use the same redaction as diagnostic exports."""
import logging
from logging.handlers import RotatingFileHandler

from ..log_export import sanitize_text


class RedactedFormatter(logging.Formatter):
    def __init__(self, token=""):
        super().__init__("%(asctime)s %(levelname)s %(name)s %(message)s")
        self.token = token

    def format(self, record):
        value = super().format(record)
        if self.token:
            value = value.replace(self.token, "[session redacted]")
        return sanitize_text(value)


def configure(log_dir, token=""):
    log_dir.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(log_dir / "desktop.log", maxBytes=2_000_000, backupCount=3, encoding="utf-8")
    handler.setFormatter(RedactedFormatter(token))
    logger = logging.getLogger("video_to_summary")
    logger.setLevel(logging.INFO)
    logger.addHandler(handler)
    return handler
