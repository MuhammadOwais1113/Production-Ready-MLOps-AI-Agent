"""Structured JSON logging with secret redaction."""
from __future__ import annotations

import json
import logging
import re
import sys
import time
from typing import Any

_SECRET_KEY = re.compile(r"(token|password|passwd|secret|api[_-]?key|authorization|credential)", re.I)
_SECRET_VALUE = re.compile(r"(sk-[A-Za-z0-9_\-]{8,}|Bearer\s+[A-Za-z0-9._\-]+|eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-.]+)")


def redact(obj: Any) -> Any:
    """Recursively mask secret-looking keys and values."""
    if isinstance(obj, dict):
        return {k: ("***REDACTED***" if _SECRET_KEY.search(str(k)) else redact(v)) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [redact(v) for v in obj]
    if isinstance(obj, str):
        return _SECRET_VALUE.sub("***REDACTED***", obj)
    return obj


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created)), "level": record.levelname,
                   "logger": record.name, "msg": record.getMessage()}
        extra = getattr(record, "fields", None)
        if extra:
            payload.update(redact(extra))
        return json.dumps(payload, default=str)


def get_logger(name: str = "mlops_agent") -> logging.Logger:
    logger = logging.getLogger(name)
    if not any(getattr(h, "_mlops", False) for h in logger.handlers):
        h = logging.StreamHandler(sys.stderr)
        h._mlops = True  # type: ignore[attr-defined]
        h.setFormatter(JsonFormatter())
        logger.addHandler(h)
        logger.setLevel(logging.INFO)
    return logger


def log_event(logger: logging.Logger, msg: str, **fields: Any) -> None:
    logger.info(msg, extra={"fields": fields})
