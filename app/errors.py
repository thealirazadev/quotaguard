"""Application error types and the handlers that render the single error envelope."""

import logging

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.requests import Request

from app.schemas.errors import ErrorBody, ErrorOut

logger = logging.getLogger("quotaguard.errors")


class AppError(Exception):
    """Base for errors that map onto a documented status code and error code."""

    status_code = 500
    code = "internal_error"

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class ValidationError(AppError):
    status_code = 422
    code = "validation_error"


class UnauthorizedError(AppError):
    status_code = 401
    code = "unauthorized"


class NotFoundError(AppError):
    status_code = 404
    code = "not_found"


class ConflictError(AppError):
    status_code = 409
    code = "conflict"


class InternalError(AppError):
    status_code = 500
    code = "internal_error"


# Framework-raised statuses (routing, method mismatch) mapped onto our stable codes
# and friendly sentences; application code raises AppError instead.
_STATUS_CODES = {401: "unauthorized", 403: "unauthorized", 404: "not_found", 409: "conflict"}
_STATUS_MESSAGES = {
    401: "Authentication is required for this route.",
    403: "Authentication is required for this route.",
    404: "The requested resource does not exist.",
    405: "That method is not allowed on this route.",
}


def error_response(status_code: int, code: str, message: str) -> JSONResponse:
    body = ErrorOut(error=ErrorBody(code=code, message=message))
    return JSONResponse(status_code=status_code, content=body.model_dump())


def _validation_message(exc: RequestValidationError) -> str:
    """Turn the first pydantic error into one friendly sentence without internals."""
    errors = exc.errors()
    if not errors:
        return "The request body is invalid."
    first = errors[0]
    # Keep named parts only: a malformed body reports a character offset, which
    # would otherwise render as a field called "1".
    location = [
        part
        for part in first.get("loc", ())
        if isinstance(part, str) and part not in ("body", "query", "path")
    ]
    field = ".".join(location)
    detail = str(first.get("msg", "is invalid")).rstrip(".")
    if not field:
        return f"{detail}."
    return f"{field}: {detail}."


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def handle_app_error(request: Request, exc: AppError) -> JSONResponse:
        logger.warning(
            "request rejected",
            extra={
                "route": request.url.path,
                "error_code": exc.code,
                "status_code": exc.status_code,
            },
        )
        return error_response(exc.status_code, exc.code, exc.message)

    @app.exception_handler(RequestValidationError)
    async def handle_request_validation(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        message = _validation_message(exc)
        logger.warning(
            "request rejected",
            extra={"route": request.url.path, "error_code": "validation_error", "status_code": 422},
        )
        return error_response(422, "validation_error", message)

    @app.exception_handler(StarletteHTTPException)
    async def handle_http_exception(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        code = _STATUS_CODES.get(exc.status_code, "internal_error")
        message = _STATUS_MESSAGES.get(exc.status_code, "The request could not be completed.")
        return error_response(exc.status_code, code, message)

    @app.exception_handler(Exception)
    async def handle_unexpected(request: Request, exc: Exception) -> JSONResponse:
        logger.exception(
            "unhandled error",
            extra={"route": request.url.path, "error_code": "internal_error", "status_code": 500},
        )
        return error_response(500, "internal_error", "Something went wrong on our side.")
