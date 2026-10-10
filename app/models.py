"""
ORM-модели (SQLAlchemy 2.0) — единственный источник схемы базы данных.

Таблицы создаются приложением при первом успешном подключении к
PostgreSQL (Base.metadata.create_all, см. db_client.py). Миграций нет:
create_all не меняет уже существующие таблицы, поэтому изменение колонок
здесь не затронет живую базу (см. known-issues.md, K26).
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Index, Text, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class Counter(Base):
    """Именованные счётчики; 'global:visits' — счётчик эндпоинта /visits."""

    __tablename__ = "whoami_counters"

    name: Mapped[str] = mapped_column(Text, primary_key=True)
    value: Mapped[int] = mapped_column(BigInteger)


class RecentRequest(Base):
    """Лог последних запросов к /visits (обрезается до N записей при вставке)."""

    __tablename__ = "whoami_recent_requests"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    node: Mapped[str] = mapped_column(Text)
    client: Mapped[str] = mapped_column(Text)


class SessionRow(Base):
    """Пользовательская сессия; срок жизни — колонка expires_at (скользящий TTL)."""

    __tablename__ = "whoami_sessions"
    __table_args__ = (Index("whoami_sessions_expires_idx", "expires_at"),)

    session_id: Mapped[str] = mapped_column(Text, primary_key=True)
    hits: Mapped[int] = mapped_column(BigInteger)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
