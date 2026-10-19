"""Logging with credential/signing-material redaction."""

from __future__ import annotations

import logging
import re

PATTERNS = [
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S), "[REDACTED PEM]"),
    (re.compile(r"(?i)(authorization\s*[:=]\s*bearer\s+)[A-Za-z0-9._\-]+"), r"\1[REDACTED]"),
    (
        re.compile(
            r"(?i)((?:api[_-]?key|secret|private[_-]?key|passphrase|token|password)[\"']?\s*[:=]\s*[\"']?)"
            r"[^\s\"',}]+"
        ),
        r"\1[REDACTED]",
    ),  # fmt: skip
    (re.compile(r"\b0x[0-9a-fA-F]{64}\b"), "[REDACTED HEX KEY]"),
    (re.compile(r"\b[1-9A-HJ-NP-Za-km-z]{86,90}\b"), "[REDACTED BASE58 KEY]"),
    (re.compile(r"\[(?:\s*\d{1,3}\s*,){63}\s*\d{1,3}\s*\]"), "[REDACTED KEYPAIR BYTES]"),
]


def redact(text: str) -> str:
    for pattern, repl in PATTERNS:
        text = pattern.sub(repl, text)
    return text


class RedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        record.msg = redact(msg)
        record.args = ()
        return True


def setup_logging(level: int = logging.INFO) -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    handler.addFilter(RedactingFilter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
