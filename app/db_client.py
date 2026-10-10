"""
Обёртка над SQLAlchemy 2.0 (async ORM, драйвер asyncpg) с коротким таймаутом
и единой точкой обработки ошибок подключения.

Устойчивость к сбоям: если PostgreSQL временно недоступен (например, узел
хранилища перезапускается), приложение НЕ должно падать целиком — должны
продолжать работать эндпоинты, не зависящие от хранилища (/, /api, /health),
а эндпоинты, работающие с данными, обязаны вернуть внятную ошибку 503.

Engine создаётся лениво при первом обращении к данным, поэтому приложение
стартует, даже если база ещё не поднялась. Схема (таблицы из models.py)
создаётся там же идемпотентно под advisory-локом, чтобы две ноды, стартующие
одновременно, не конфликтовали.

Эндпоинты не работают с сессиями и SQL напрямую: каждая операция — метод
DatabaseManager, который целиком выполняется в одной транзакции.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import timedelta
from typing import AsyncIterator

from sqlalchemy import case, delete, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.engine import URL
from sqlalchemy.exc import DBAPIError, SQLAlchemyError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from .config import settings
from .models import Base, Counter, RecentRequest, SessionRow


class StorageUnavailable(Exception):
    """Хранилище недоступно (таймаут, обрыв соединения, ошибка SQL и т.п.)."""


# SQLAlchemyError - ошибки драйвера/SQL и исчерпание пула (в т.ч. pool_timeout),
# OSError - сокет/DNS при установке соединения (SQLAlchemy их не оборачивает),
# TimeoutError - таймауты на уровне asyncio.
_DB_ERRORS = (SQLAlchemyError, OSError, asyncio.TimeoutError)

# Произвольная константа для pg_advisory_xact_lock при создании схемы.
_SCHEMA_LOCK_ID = 727_001

_VISITS_COUNTER = "global:visits"


def _reason(exc: BaseException) -> str:
    # str(DBAPIError) содержит текст SQL и значения параметров (например,
    # session_id) - в ответ 503 отдаём только сообщение самого драйвера.
    if isinstance(exc, DBAPIError) and exc.orig is not None:
        exc = exc.orig
    # У TimeoutError пустое сообщение - показываем хотя бы тип.
    return str(exc) or type(exc).__name__


class DatabaseManager:
    def __init__(self) -> None:
        self._engine: AsyncEngine | None = None
        self._sessionmaker: async_sessionmaker[AsyncSession] | None = None
        self._lock = asyncio.Lock()

    @staticmethod
    def _create_engine() -> AsyncEngine:
        url = URL.create(
            "postgresql+asyncpg",
            username=settings.postgres_user,
            password=settings.postgres_password,
            host=settings.postgres_host,
            port=settings.postgres_port,
            database=settings.postgres_db,
        )
        return create_async_engine(
            url,
            pool_size=settings.postgres_pool_max,
            max_overflow=0,
            # Ожидание свободного соединения из пула.
            pool_timeout=settings.postgres_connect_timeout,
            # Проверка соединения при выдаче из пула: после рестарта PostgreSQL
            # мёртвые соединения заменяются, а не отдаются запросу.
            pool_pre_ping=True,
            connect_args={
                "timeout": settings.postgres_connect_timeout,
                "command_timeout": settings.postgres_command_timeout,
            },
        )

    async def _get_sessionmaker(self) -> async_sessionmaker[AsyncSession]:
        if self._sessionmaker is not None:
            return self._sessionmaker
        async with self._lock:
            if self._sessionmaker is None:
                engine = self._create_engine()
                try:
                    await self._ensure_schema(engine)
                except BaseException:
                    await engine.dispose()
                    raise
                self._engine = engine
                self._sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
        return self._sessionmaker

    @staticmethod
    async def _ensure_schema(engine: AsyncEngine) -> None:
        async with engine.begin() as conn:
            await conn.execute(select(func.pg_advisory_xact_lock(_SCHEMA_LOCK_ID)))
            await conn.run_sync(Base.metadata.create_all)

    @asynccontextmanager
    async def _session(self) -> AsyncIterator[AsyncSession]:
        """
        Сессия ORM в одной транзакции (commit при выходе, rollback при ошибке);
        любые ошибки БД превращаются в StorageUnavailable.
        """
        try:
            maker = await self._get_sessionmaker()
            async with maker() as session, session.begin():
                yield session
        except _DB_ERRORS as exc:
            raise StorageUnavailable(_reason(exc)) from exc

    async def ping(self) -> bool:
        try:
            async with self._session() as session:
                return (await session.scalar(select(1))) == 1
        except StorageUnavailable:
            return False

    async def record_visit(self, node: str, client: str, max_recent: int = 20) -> int:
        """
        Атомарно (одна транзакция): увеличивает глобальный счётчик, добавляет
        запись в лог запросов и обрезает лог до max_recent последних записей.
        Возвращает новое значение счётчика.
        """
        async with self._session() as session:
            total = await session.scalar(
                pg_insert(Counter)
                .values(name=_VISITS_COUNTER, value=1)
                .on_conflict_do_update(
                    index_elements=[Counter.name],
                    set_={"value": Counter.value + 1},
                )
                .returning(Counter.value)
            )
            session.add(RecentRequest(node=node, client=client))
            await session.flush()
            # Оставляем только последние max_recent записей: удаляем всё,
            # что не новее (max_recent + 1)-й записи с конца.
            cutoff = (
                select(RecentRequest.id)
                .order_by(RecentRequest.id.desc())
                .offset(max_recent)
                .limit(1)
                .scalar_subquery()
            )
            await session.execute(delete(RecentRequest).where(RecentRequest.id <= cutoff))
            return int(total)

    async def recent_visits(self, limit: int = 20) -> list[str]:
        """Последние записи лога, новые первыми: 'ISO-время node=<id> client=<ip>'."""
        async with self._session() as session:
            rows = (
                await session.scalars(
                    select(RecentRequest).order_by(RecentRequest.id.desc()).limit(limit)
                )
            ).all()
            return [f"{r.created_at.isoformat()} node={r.node} client={r.client}" for r in rows]

    async def session_hit(self, session_id: str, ttl: int) -> int:
        """Атомарно увеличивает счётчик сессии и продлевает её срок жизни на ttl секунд."""
        new_expiry = func.now() + timedelta(seconds=ttl)
        async with self._session() as session:
            hits = await session.scalar(
                pg_insert(SessionRow)
                .values(session_id=session_id, hits=1, expires_at=new_expiry)
                .on_conflict_do_update(
                    index_elements=[SessionRow.session_id],
                    set_={
                        # Скользящий TTL: если строка уже просрочена, но фоновая
                        # очистка её ещё не удалила, счётчик начинается с 1.
                        "hits": case(
                            (SessionRow.expires_at <= func.now(), 1),
                            else_=SessionRow.hits + 1,
                        ),
                        "expires_at": new_expiry,
                    },
                )
                .returning(SessionRow.hits)
            )
            return int(hits)

    async def purge_expired_sessions(self) -> int:
        """Удаляет просроченные сессии; возвращает число удалённых строк."""
        async with self._session() as session:
            result = await session.execute(
                delete(SessionRow).where(SessionRow.expires_at <= func.now())
            )
            return int(result.rowcount)

    async def close(self) -> None:
        engine, self._engine, self._sessionmaker = self._engine, None, None
        if engine is not None:
            try:
                await asyncio.wait_for(engine.dispose(), timeout=2.0)
            except (SQLAlchemyError, OSError, asyncio.TimeoutError):
                # Закрываемся при остановке приложения: если БД уже недоступна,
                # просто бросаем пул, не дожидаясь корректного закрытия.
                await engine.dispose(close=False)


db_manager = DatabaseManager()
