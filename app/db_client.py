"""
Обёртка над asyncpg с коротким таймаутом и единой точкой обработки
ошибок подключения.

Устойчивость к сбоям: если PostgreSQL временно недоступен (например, узел
хранилища перезапускается), приложение НЕ должно падать целиком — должны
продолжать работать эндпоинты, не зависящие от хранилища (/, /api, /health),
а эндпоинты, работающие с данными, обязаны вернуть внятную ошибку 503.

Пул соединений создаётся лениво при первом обращении к данным, поэтому
приложение стартует, даже если база ещё не поднялась. Схема (таблицы)
создаётся там же идемпотентно под advisory-локом, чтобы две ноды, стартующие
одновременно, не конфликтовали.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import AsyncIterator

import asyncpg

from .config import settings


class StorageUnavailable(Exception):
    """Хранилище недоступно (таймаут, обрыв соединения, ошибка SQL и т.п.)."""


# asyncpg.PostgresError - ошибки сервера (в т.ч. класс 08 - проблемы соединения),
# InterfaceError - ошибки клиентской стороны (соединение закрыто и т.п.),
# OSError - сокет/DNS, TimeoutError - таймауты пула и запросов.
_DB_ERRORS = (asyncpg.PostgresError, asyncpg.InterfaceError, OSError, asyncio.TimeoutError)

# Произвольная константа для pg_advisory_xact_lock при создании схемы.
_SCHEMA_LOCK_ID = 727_001

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS whoami_counters (
    name  text   PRIMARY KEY,
    value bigint NOT NULL
);
CREATE TABLE IF NOT EXISTS whoami_recent_requests (
    id         bigserial   PRIMARY KEY,
    created_at timestamptz NOT NULL DEFAULT now(),
    node       text        NOT NULL,
    client     text        NOT NULL
);
CREATE TABLE IF NOT EXISTS whoami_sessions (
    session_id text        PRIMARY KEY,
    hits       bigint      NOT NULL,
    expires_at timestamptz NOT NULL
);
CREATE INDEX IF NOT EXISTS whoami_sessions_expires_idx ON whoami_sessions (expires_at);
"""

_VISITS_COUNTER = "global:visits"

_INCR_COUNTER_SQL = """
INSERT INTO whoami_counters (name, value) VALUES ($1, 1)
ON CONFLICT (name) DO UPDATE SET value = whoami_counters.value + 1
RETURNING value
"""

_INSERT_RECENT_SQL = "INSERT INTO whoami_recent_requests (node, client) VALUES ($1, $2)"

# Оставляем только последние $1 записей: удаляем всё, что не новее (N+1)-й с конца.
_TRIM_RECENT_SQL = """
DELETE FROM whoami_recent_requests
WHERE id <= (SELECT id FROM whoami_recent_requests ORDER BY id DESC OFFSET $1 LIMIT 1)
"""

_SELECT_RECENT_SQL = """
SELECT created_at, node, client FROM whoami_recent_requests
ORDER BY id DESC LIMIT $1
"""

# Скользящий TTL: каждое обращение продлевает срок жизни. Если строка уже
# просрочена, но фоновая очистка её ещё не удалила, счётчик начинается с 1.
_SESSION_HIT_SQL = """
INSERT INTO whoami_sessions (session_id, hits, expires_at)
VALUES ($1, 1, now() + make_interval(secs => $2::double precision))
ON CONFLICT (session_id) DO UPDATE SET
    hits = CASE WHEN whoami_sessions.expires_at <= now() THEN 1
                ELSE whoami_sessions.hits + 1 END,
    expires_at = now() + make_interval(secs => $2::double precision)
RETURNING hits
"""

_PURGE_SESSIONS_SQL = "DELETE FROM whoami_sessions WHERE expires_at <= now()"


def _reason(exc: BaseException) -> str:
    # У TimeoutError пустое сообщение - показываем хотя бы тип.
    return str(exc) or type(exc).__name__


class DatabaseManager:
    def __init__(self) -> None:
        self._pool: asyncpg.Pool | None = None
        self._lock = asyncio.Lock()

    async def _get_pool(self) -> asyncpg.Pool:
        if self._pool is not None:
            return self._pool
        async with self._lock:
            if self._pool is None:
                pool = await asyncpg.create_pool(
                    host=settings.postgres_host,
                    port=settings.postgres_port,
                    database=settings.postgres_db,
                    user=settings.postgres_user,
                    password=settings.postgres_password,
                    min_size=1,
                    max_size=settings.postgres_pool_max,
                    timeout=settings.postgres_connect_timeout,
                    command_timeout=settings.postgres_command_timeout,
                )
                try:
                    await self._ensure_schema(pool)
                except BaseException:
                    pool.terminate()
                    raise
                self._pool = pool
        return self._pool

    @staticmethod
    async def _ensure_schema(pool: asyncpg.Pool) -> None:
        async with pool.acquire(timeout=settings.postgres_connect_timeout) as conn:
            async with conn.transaction():
                await conn.execute("SELECT pg_advisory_xact_lock($1)", _SCHEMA_LOCK_ID)
                await conn.execute(_SCHEMA_SQL)

    @asynccontextmanager
    async def _conn(self) -> AsyncIterator[asyncpg.Connection]:
        """Соединение из пула; любые ошибки БД превращаются в StorageUnavailable."""
        try:
            pool = await self._get_pool()
            async with pool.acquire(timeout=settings.postgres_connect_timeout) as conn:
                yield conn
        except _DB_ERRORS as exc:
            raise StorageUnavailable(_reason(exc)) from exc

    async def ping(self) -> bool:
        try:
            async with self._conn() as conn:
                return (await conn.fetchval("SELECT 1")) == 1
        except StorageUnavailable:
            return False

    async def record_visit(self, node: str, client: str, max_recent: int = 20) -> int:
        """
        Атомарно (одна транзакция): увеличивает глобальный счётчик, добавляет
        запись в лог запросов и обрезает лог до max_recent последних записей.
        Возвращает новое значение счётчика.
        """
        async with self._conn() as conn:
            async with conn.transaction():
                total = await conn.fetchval(_INCR_COUNTER_SQL, _VISITS_COUNTER)
                await conn.execute(_INSERT_RECENT_SQL, node, client)
                await conn.execute(_TRIM_RECENT_SQL, max_recent)
                return int(total)

    async def recent_visits(self, limit: int = 20) -> list[str]:
        """Последние записи лога, новые первыми: 'ISO-время node=<id> client=<ip>'."""
        async with self._conn() as conn:
            rows = await conn.fetch(_SELECT_RECENT_SQL, limit)
        return [f"{r['created_at'].isoformat()} node={r['node']} client={r['client']}" for r in rows]

    async def session_hit(self, session_id: str, ttl: int) -> int:
        """Атомарно увеличивает счётчик сессии и продлевает её срок жизни на ttl секунд."""
        async with self._conn() as conn:
            return int(await conn.fetchval(_SESSION_HIT_SQL, session_id, float(ttl)))

    async def purge_expired_sessions(self) -> int:
        """Удаляет просроченные сессии; возвращает число удалённых строк."""
        async with self._conn() as conn:
            status = await conn.execute(_PURGE_SESSIONS_SQL)
        return int(status.split()[-1])

    async def close(self) -> None:
        pool, self._pool = self._pool, None
        if pool is not None:
            try:
                await asyncio.wait_for(pool.close(), timeout=2.0)
            except Exception:
                # Закрываемся при остановке приложения: если БД уже недоступна,
                # просто обрываем соединения.
                pool.terminate()


db_manager = DatabaseManager()
