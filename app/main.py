"""Application factory: logging, middleware, exception handlers, routers."""

from fastapi import FastAPI

from app.config import get_settings
from app.errors import register_exception_handlers
from app.logging import RequestIdMiddleware, configure_logging
from app.routers import health


def create_app() -> FastAPI:
    settings = get_settings()
    configure_logging(settings.log_level)

    app = FastAPI(
        title="quotaguard",
        version="0.1.0",
        description="API rate limiting and quota decision service.",
    )
    app.add_middleware(RequestIdMiddleware)
    register_exception_handlers(app)
    app.include_router(health.router)
    return app


app = create_app()
