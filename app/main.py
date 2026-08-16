"""Application factory: logging, middleware, exception handlers, routers."""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, AsyncExitStack

from fastapi import FastAPI
from redis.exceptions import RedisError

from app import redis_client
from app.config import get_settings
from app.errors import register_exception_handlers
from app.logging import RequestIdMiddleware, configure_logging
from app.routers import admin, check, health
from app.scheduler import scheduler_lifespan

logger = logging.getLogger("quotaguard.app")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Preload Lua scripts, start scheduler loops. A Redis outage at startup must not stop."""
    async with AsyncExitStack() as stack:
        try:
            await redis_client.get_registry().load_all()
        except (RedisError, RuntimeError, OSError) as exc:
            logger.warning("lua scripts not preloaded: %s", exc.__class__.__name__)

        await stack.enter_async_context(scheduler_lifespan())

        try:
            yield
        finally:
            await redis_client.close()


def create_app() -> FastAPI:
    settings = get_settings()
    configure_logging(settings.log_level)

    app = FastAPI(
        title="quotaguard",
        version="0.1.0",
        description="API rate limiting and quota decision service.",
        lifespan=lifespan,
    )
    app.add_middleware(RequestIdMiddleware)
    register_exception_handlers(app)
    app.include_router(health.router)
    app.include_router(check.router)
    app.include_router(admin.router)
    return app


app = create_app()
