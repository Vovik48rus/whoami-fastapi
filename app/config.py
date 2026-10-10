"""
Конфигурация приложения.

Все параметры задаются через переменные окружения, чтобы одинаковый образ
можно было запускать как несколько независимых нод с разными идентификаторами
(NODE_NAME) и общим внешним хранилищем (PostgreSQL), не пересобирая контейнер.
"""
import os
from dataclasses import dataclass


def _get_bool(name: str, default: bool) -> bool:
    val = os.getenv(name)
    if val is None:
        return default
    return val.strip().lower() in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class Settings:
    # Человекочитаемое имя ноды. Если не задано - будет использован hostname
    # контейнера (обычно совпадает с container ID в Docker).
    node_name: str = os.getenv("NODE_NAME", "")

    # Порт, на котором приложение слушает HTTP. TLS-терминацией занимается
    # Apache/Nginx перед этим приложением, поэтому uvicorn запускается без
    # сертификатов (--ssl-keyfile/--ssl-certfile).
    port: int = int(os.getenv("PORT", "8000"))

    # --- Внешнее хранилище (PostgreSQL) ----------------------------------
    # PostgreSQL вынесен в отдельный узел/контейнер, не совпадающий с нодами
    # приложения. Потеря любой из нод FastAPI не приводит к потере данных
    # счётчиков и сессий.
    postgres_host: str = os.getenv("POSTGRES_HOST", "postgres")
    postgres_port: int = int(os.getenv("POSTGRES_PORT", "5432"))
    postgres_db: str = os.getenv("POSTGRES_DB", "whoami")
    postgres_user: str = os.getenv("POSTGRES_USER", "whoami")
    postgres_password: str | None = os.getenv("POSTGRES_PASSWORD") or None

    # Таймауты, чтобы при падении PostgreSQL приложение не подвисало,
    # а быстро отвечало ошибкой 503 конкретному эндпоинту.
    # connect_timeout ограничивает установку соединения и ожидание свободного
    # соединения из пула; command_timeout - выполнение запроса. Он больше,
    # потому что запросы к /visits выстраиваются в очередь на блокировке одной
    # строки счётчика: под нагрузкой ожидание доходит до пары секунд.
    postgres_connect_timeout: float = float(os.getenv("POSTGRES_CONNECT_TIMEOUT", "1.0"))
    postgres_command_timeout: float = float(os.getenv("POSTGRES_COMMAND_TIMEOUT", "3.0"))

    # Максимальный размер пула соединений на одну ноду. Учитывайте
    # max_connections самого PostgreSQL (по умолчанию 100) * число нод.
    postgres_pool_max: int = int(os.getenv("POSTGRES_POOL_MAX", "10"))

    # TTL пользовательской сессии в секундах (сессия хранится в PostgreSQL, а не
    # в памяти процесса и не в локальных файлах). В PostgreSQL нет встроенного
    # TTL: срок жизни задаётся колонкой expires_at, а просроченные строки
    # периодически удаляются фоновой задачей каждой ноды.
    session_ttl_seconds: int = int(os.getenv("SESSION_TTL_SECONDS", "3600"))
    session_purge_interval_seconds: int = int(os.getenv("SESSION_PURGE_INTERVAL_SECONDS", "60"))
    session_cookie_name: str = os.getenv("SESSION_COOKIE_NAME", "whoami_session")

    # Cookie должна работать и через HTTP, и через HTTPS-терминацию на
    # реверс-прокси — приложение само TLS не видит, поэтому secure=False.
    session_cookie_secure: bool = _get_bool("SESSION_COOKIE_SECURE", False)


settings = Settings()
