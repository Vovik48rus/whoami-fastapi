"""
Обёртка над redis.asyncio с коротким таймаутом и единой точкой обработки
ошибок подключения.

Важно для лабораторной: если Redis временно недоступен (например, нода
хранилища перезапускается), приложение НЕ должно падать целиком — должны
продолжать работать эндпоинты, не зависящие от хранилища (/, /api, /health),
а эндпоинты, работающие с данными, обязаны вернуть внятную ошибку 503.
"""
from __future__ import annotations

import redis.asyncio as redis
from redis.exceptions import RedisError

from .config import settings


class RedisUnavailable(Exception):
    """Хранилище недоступно (таймаут, обрыв соединения и т.п.)."""


class RedisManager:
    def __init__(self) -> None:
        self._pool: redis.ConnectionPool | None = None

    def _get_pool(self) -> redis.ConnectionPool:
        if self._pool is None:
            self._pool = redis.ConnectionPool(
                host=settings.redis_host,
                port=settings.redis_port,
                db=settings.redis_db,
                password=settings.redis_password,
                socket_connect_timeout=settings.redis_connect_timeout,
                socket_timeout=settings.redis_socket_timeout,
                decode_responses=True,
                max_connections=20,
            )
        return self._pool

    def client(self) -> redis.Redis:
        return redis.Redis(connection_pool=self._get_pool())

    async def ping(self) -> bool:
        try:
            return bool(await self.client().ping())
        except (RedisError, OSError):
            return False

    async def incr(self, key: str) -> int:
        try:
            return await self.client().incr(key)
        except (RedisError, OSError) as exc:
            raise RedisUnavailable(str(exc)) from exc

    async def get(self, key: str) -> str | None:
        try:
            return await self.client().get(key)
        except (RedisError, OSError) as exc:
            raise RedisUnavailable(str(exc)) from exc

    async def setex(self, key: str, ttl: int, value: str) -> None:
        try:
            await self.client().setex(key, ttl, value)
        except (RedisError, OSError) as exc:
            raise RedisUnavailable(str(exc)) from exc

    async def incr_with_ttl(self, key: str, ttl: int) -> int:
        """Атомарно увеличивает счётчик и (пере)устанавливает TTL."""
        try:
            client = self.client()
            async with client.pipeline(transaction=True) as pipe:
                pipe.incr(key)
                pipe.expire(key, ttl)
                result, _ = await pipe.execute()
                return int(result)
        except (RedisError, OSError) as exc:
            raise RedisUnavailable(str(exc)) from exc

    async def lpush_capped(self, key: str, value: str, max_len: int = 20) -> None:
        try:
            client = self.client()
            async with client.pipeline(transaction=True) as pipe:
                pipe.lpush(key, value)
                pipe.ltrim(key, 0, max_len - 1)
                await pipe.execute()
        except (RedisError, OSError) as exc:
            raise RedisUnavailable(str(exc)) from exc

    async def lrange(self, key: str, start: int, end: int) -> list[str]:
        try:
            return await self.client().lrange(key, start, end)
        except (RedisError, OSError) as exc:
            raise RedisUnavailable(str(exc)) from exc


redis_manager = RedisManager()
