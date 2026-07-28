"""Redis connection pool and the Lua script registry.

Every admission mutation happens inside a script loaded from ``app/lua/`` and
invoked by SHA. Redis empties its script cache on restart, so a NOSCRIPT reply is
answered with one reload-and-retry rather than treated as an outage.
"""

import logging
from pathlib import Path
from typing import Any

import redis.asyncio as redis_asyncio
from redis.exceptions import NoScriptError, RedisError

from app.config import get_settings

logger = logging.getLogger("quotaguard.redis")

LUA_DIR = Path(__file__).resolve().parent / "lua"


class ScriptRegistry:
    """Holds the SHA of each script in ``app/lua`` and reloads it on NOSCRIPT."""

    def __init__(self, client: redis_asyncio.Redis) -> None:
        self._client = client
        self._shas: dict[str, str] = {}

    @staticmethod
    def _source(name: str) -> str:
        path = LUA_DIR / f"{name}.lua"
        try:
            return path.read_text(encoding="utf-8")
        except OSError as exc:
            raise RuntimeError(f"Lua script '{name}' could not be read from {path}.") from exc

    async def register(self, name: str) -> str:
        sha = await self._client.script_load(self._source(name))
        self._shas[name] = sha
        return sha

    async def load_all(self) -> None:
        """Load every script at startup so the first check pays no compile cost."""
        if not LUA_DIR.is_dir():
            return
        for path in sorted(LUA_DIR.glob("*.lua")):
            await self.register(path.stem)

    async def run(self, name: str, keys: list[str], args: list[Any]) -> Any:
        sha = self._shas.get(name) or await self.register(name)
        try:
            return await self._client.evalsha(sha, len(keys), *keys, *args)
        except NoScriptError:
            logger.warning("lua script missing from the redis cache, reloading once")
            sha = await self.register(name)
            return await self._client.evalsha(sha, len(keys), *keys, *args)


_client: redis_asyncio.Redis | None = None
_registry: ScriptRegistry | None = None


def get_client() -> redis_asyncio.Redis:
    """Process-wide pool. Timeouts are explicit so a stalled Redis cannot hang a check."""
    global _client
    if _client is None:
        settings = get_settings()
        timeout = settings.redis_timeout_ms / 1000
        _client = redis_asyncio.from_url(
            settings.redis_url,
            socket_timeout=timeout,
            socket_connect_timeout=timeout,
            decode_responses=False,
        )
    return _client


def get_registry() -> ScriptRegistry:
    global _registry
    if _registry is None:
        _registry = ScriptRegistry(get_client())
    return _registry


async def close() -> None:
    """Drop the pool and the cached SHAs; the next caller rebuilds both."""
    global _client, _registry
    client, _client, _registry = _client, None, None
    if client is None:
        return
    try:
        await client.aclose()
    except RedisError as exc:
        logger.warning("redis pool close failed: %s", exc.__class__.__name__)
