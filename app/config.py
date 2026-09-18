"""
Конфигурация приложения.

Все параметры задаются через переменные окружения, чтобы одинаковый образ
можно было запускать как несколько независимых нод с разными идентификаторами
(NODE_NAME) и общим внешним хранилищем (Redis), не пересобирая контейнер.
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

    # Порт, на котором приложение слушает HTTP (без TLS! см. ограничение №3 —
    # терминацией TLS занимается Apache/Nginx перед этим приложением).
    port: int = int(os.getenv("PORT", "8000"))

    # --- Внешнее хранилище (Redis) ---------------------------------------
    # Redis должен быть вынесен в отдельный узел/контейнер, не совпадающий
    # с нодами приложения. Тогда потеря любой из нод FastAPI не приводит
    # к потере данных счётчиков/сессий (ограничение №2 и №4).
    redis_host: str = os.getenv("REDIS_HOST", "redis")
    redis_port: int = int(os.getenv("REDIS_PORT", "6379"))
    redis_db: int = int(os.getenv("REDIS_DB", "0"))
    redis_password: str | None = os.getenv("REDIS_PASSWORD") or None

    # Таймауты, чтобы при падении Redis приложение не подвисало,
    # а быстро отвечало ошибкой 503 конкретному эндпоинту.
    redis_connect_timeout: float = float(os.getenv("REDIS_CONNECT_TIMEOUT", "0.5"))
    redis_socket_timeout: float = float(os.getenv("REDIS_SOCKET_TIMEOUT", "0.5"))

    # TTL пользовательской сессии в секундах (сессия хранится в Redis, а не
    # в памяти процесса и не в локальных файлах — ограничение №4).
    session_ttl_seconds: int = int(os.getenv("SESSION_TTL_SECONDS", "3600"))
    session_cookie_name: str = os.getenv("SESSION_COOKIE_NAME", "whoami_session")

    # Cookie должна работать и через HTTP, и через HTTPS-терминацию на
    # реверс-прокси — приложение само TLS не видит, поэтому secure=False.
    session_cookie_secure: bool = _get_bool("SESSION_COOKIE_SECURE", False)


settings = Settings()
