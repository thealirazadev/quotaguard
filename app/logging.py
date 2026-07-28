"""Structured JSON logging: one object per line, plus request-id propagation."""

import json
import logging
import sys
import time
import uuid
from contextvars import ContextVar

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

REQUEST_ID_HEADER = "X-Request-ID"

# Context fields copied onto a log line when the caller supplied them via `extra`.
CONTEXT_FIELDS = (
    "event",
    "request_id",
    "route",
    "status_code",
    "duration_ms",
    "key_id",
    "resource",
    "deny_layer",
    "policy",
    "month",
    "error_code",
)

_request_id: ContextVar[str | None] = ContextVar("request_id", default=None)


def get_request_id() -> str | None:
    return _request_id.get()


class JsonFormatter(logging.Formatter):
    """Renders a record as a single JSON line with the documented context fields."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created))
            + f".{int(record.msecs):03d}Z",
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        request_id = getattr(record, "request_id", None) or get_request_id()
        if request_id:
            payload["request_id"] = request_id
        for field in CONTEXT_FIELDS:
            if field == "request_id":
                continue
            value = getattr(record, field, None)
            if value is not None:
                payload[field] = value
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def configure_logging(level: str, stream=None) -> None:
    """Install the JSON handler on the root logger, replacing any previous handlers.

    The CLI passes stderr so its own stdout stays a clean machine interface.
    """
    handler = logging.StreamHandler(stream or sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    for existing in list(root.handlers):
        root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(level.upper())


class RequestIdMiddleware(BaseHTTPMiddleware):
    """Assigns or echoes X-Request-ID and logs each request at DEBUG.

    Access logging stays at DEBUG so the check hot path does not pay for a log
    line per request (docs/rules.md).
    """

    def __init__(self, app: object) -> None:
        super().__init__(app)  # type: ignore[arg-type]
        self._logger = logging.getLogger("quotaguard.request")

    async def dispatch(self, request: Request, call_next) -> Response:
        request_id = request.headers.get(REQUEST_ID_HEADER) or uuid.uuid4().hex
        token = _request_id.set(request_id)
        started = time.perf_counter()
        try:
            response = await call_next(request)
        finally:
            _request_id.reset(token)
        duration_ms = round((time.perf_counter() - started) * 1000, 2)
        response.headers[REQUEST_ID_HEADER] = request_id
        self._logger.debug(
            "request handled",
            extra={
                "request_id": request_id,
                "route": request.url.path,
                "status_code": response.status_code,
                "duration_ms": duration_ms,
            },
        )
        return response
