"""Redact credentials even when a server error contains a WebSocket URL."""

import logging
import re


class CredentialSafeFormatter(logging.Formatter):
    def format(self, record):
        value = super().format(record)
        value = re.sub(
            r"(?i)(\b(?:access_token|refresh_token|dev_auth|api_key|apikey|password|token)=)[^\s&\"'<>]+",
            r"\1[REDACTED]", value,
        )
        value = re.sub(r"(?i)(\bBearer\s+)[A-Za-z0-9._~+/=-]+", r"\1[REDACTED]", value)
        return re.sub(r"(://[^\s/:@]+:)[^\s@/]+(@)", r"\1[REDACTED]\2", value)
