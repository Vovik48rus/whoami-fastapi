"""
whoami-fastapi
==============

Аналог https://github.com/traefik/whoami на FastAPI: HTTP-сервис, который
отвечает на каждый запрос идентификатором обработавшей его ноды (hostname,
NODE_NAME, PID, ID контейнера) и эхом заголовков запроса. Предназначен для
развёртывания за реверс-прокси (Apache/Nginx) с балансировкой нагрузки.

Ключевые свойства:

- Работает как несколько независимых инстансов (нод) за балансировщиком —
  каждый ответ однозначно показывает, какая нода его обработала.
- Общее состояние (счётчики, лог запросов, сессии) хранится не в памяти
  процесса и не в файле на диске ноды, а во внешнем PostgreSQL. Падение одной
  ноды не приводит к потере данных и не мешает остальным нодам продолжать
  работать с тем же состоянием. Если недоступен сам PostgreSQL — зависящие от
  него эндпоинты возвращают 503, а не роняют всё приложение.
- Приложение слушает только plain HTTP (uvicorn без ssl_certfile/ssl_keyfile).
  TLS-терминация выполняется на Apache/Nginx перед этим приложением.
- Пользовательская сессия идентифицируется cookie со случайным ID, а её
  данные (счётчик визитов сессии) хранятся в PostgreSQL с TTL (колонка
  expires_at) — не в памяти процесса и не в локальном файле.
"""
from __future__ import annotations

import asyncio
import contextlib
import os
import platform
import socket
import sys
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse, PlainTextResponse, HTMLResponse
from pydantic import BaseModel

from .config import settings
from .db_client import db_manager, StorageUnavailable


async def _purge_sessions_loop() -> None:
    """
    Фоновая очистка просроченных сессий (в PostgreSQL нет встроенного TTL).
    Каждая нода запускает свою копию; DELETE идемпотентен, поэтому ноды
    не мешают друг другу. Недоступность БД не должна убивать задачу.
    """
    while True:
        await asyncio.sleep(settings.session_purge_interval_seconds)
        with contextlib.suppress(StorageUnavailable):
            await db_manager.purge_expired_sessions()


@asynccontextmanager
async def lifespan(_: FastAPI):
    purge_task = asyncio.create_task(_purge_sessions_loop())
    try:
        yield
    finally:
        purge_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await purge_task
        await db_manager.close()


app = FastAPI(
    title="whoami-fastapi",
    description="FastAPI-аналог traefik/whoami: идентификация ноды и эхо заголовков запроса",
    version="1.0.0",
    lifespan=lifespan,
)

START_TIME = time.monotonic()
START_TIME_ISO = datetime.now(timezone.utc).isoformat()
HOSTNAME = socket.gethostname()
PID = os.getpid()


# --------------------------------------------------------------------------- #
# Идентификация ноды
# --------------------------------------------------------------------------- #
def get_container_id() -> str | None:
    """ID контейнера Docker/containerd из cgroup, если приложение внутри контейнера."""
    try:
        with open("/proc/self/cgroup") as f:
            for line in f:
                line = line.strip()
                if "/docker/" in line or "/containerd/" in line or "docker-" in line:
                    part = line.rsplit("/", 1)[-1].replace(".scope", "")
                    if len(part) >= 12:
                        return part[:12]
    except (FileNotFoundError, PermissionError):
        pass
    return None


def get_local_ips() -> list[str]:
    ips: set[str] = set()
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None):
            ips.add(info[4][0])
    except socket.gaierror:
        pass
    ips.discard("127.0.0.1")
    return sorted(ips) or ["127.0.0.1"]


CONTAINER_ID = get_container_id()
# Итоговый идентификатор ноды: явное имя из окружения > ID контейнера > hostname.
NODE_ID = settings.node_name or CONTAINER_ID or HOSTNAME


def node_identity() -> dict:
    return {
        "node_name": NODE_ID,
        "hostname": HOSTNAME,
        "container_id": CONTAINER_ID,
        "pid": PID,
        "ips": get_local_ips(),
    }


# --------------------------------------------------------------------------- #
# Middleware: помечаем каждый ответ заголовком с идентификатором ноды —
# удобно смотреть в devtools/curl -i, какой backend ответил через балансировщик.
# --------------------------------------------------------------------------- #
@app.middleware("http")
async def add_node_headers(request: Request, call_next):
    response: Response = await call_next(request)
    response.headers["X-Node-Name"] = str(NODE_ID)
    response.headers["X-Node-Hostname"] = HOSTNAME
    response.headers["X-Node-Pid"] = str(PID)
    return response


# --------------------------------------------------------------------------- #
# Базовый whoami — текстовый и JSON вывод в духе traefik/whoami
# --------------------------------------------------------------------------- #
@app.get("/", response_class=PlainTextResponse)
async def whoami_text(request: Request) -> str:
    lines = [
        f"Hostname: {HOSTNAME}",
        f"NodeName: {NODE_ID}",
        f"ContainerID: {CONTAINER_ID or '-'}",
        f"PID: {PID}",
    ]
    for ip in get_local_ips():
        lines.append(f"IP: {ip}")

    client_host = request.client.host if request.client else "-"
    client_port = request.client.port if request.client else "-"
    lines.append(f"RemoteAddr: {client_host}:{client_port}")

    lines.append(f"{request.method} {request.url.path} HTTP/{request.scope.get('http_version', '1.1')}")
    lines.append(f"Host: {request.headers.get('host', '-')}")
    for name, value in request.headers.items():
        if name.lower() == "host":
            continue
        lines.append(f"{name.title()}: {value}")

    return "\n".join(lines) + "\n"


@app.get("/api")
async def whoami_json(request: Request) -> dict:
    client = request.client
    return {
        **node_identity(),
        "request": {
            "method": request.method,
            "path": request.url.path,
            "query": str(request.url.query) or None,
            "remote_addr": client.host if client else None,
            "remote_port": client.port if client else None,
            "headers": dict(request.headers),
        },
        "server_time_utc": datetime.now(timezone.utc).isoformat(),
        "uptime_seconds": round(time.monotonic() - START_TIME, 3),
    }


@app.get("/ui", response_class=HTMLResponse)
async def whoami_ui(request: Request) -> str:
    data = node_identity()
    rows = "".join(f"<tr><th>{k}</th><td>{v}</td></tr>" for k, v in data.items() if k != "ips")
    ips = ", ".join(data["ips"])
    return f"""<!DOCTYPE html>
<html lang="ru"><head><meta charset="utf-8">
<title>whoami-fastapi — {data['node_name']}</title>
<style>
 body {{ font-family: monospace; background:#111; color:#0f0; padding:2rem; }}
 table {{ border-collapse: collapse; }}
 th, td {{ border: 1px solid #0f0; padding: 4px 10px; text-align:left; }}
 th {{ color:#8f8; }}
 h1 {{ color:#fff; }}
</style></head>
<body>
<h1>whoami-fastapi</h1>
<p>Этот backend обработал ваш запрос:</p>
<table>{rows}<tr><th>ips</th><td>{ips}</td></tr></table>
<p><a href="/api" style="color:#0ff">/api</a> ·
<a href="/health" style="color:#0ff">/health</a> ·
<a href="/visits" style="color:#0ff">/visits</a> ·
<a href="/session" style="color:#0ff">/session</a></p>
</body></html>"""


# --------------------------------------------------------------------------- #
# Health-check'и для балансировщика / оркестратора
# --------------------------------------------------------------------------- #
@app.get("/health")
async def health_liveness() -> dict:
    """Liveness: отвечает, пока жив сам процесс — не зависит от PostgreSQL."""
    return {"status": "ok", "node_name": NODE_ID, "hostname": HOSTNAME}


@app.get("/health/ready")
async def health_readiness() -> JSONResponse:
    """Readiness: дополнительно проверяет доступность внешнего хранилища."""
    db_ok = await db_manager.ping()
    status_code = 200 if db_ok else 503
    return JSONResponse(
        status_code=status_code,
        content={"status": "ok" if db_ok else "degraded", "node_name": NODE_ID, "postgres": db_ok},
    )


# --------------------------------------------------------------------------- #
# Общее состояние во внешнем хранилище (PostgreSQL) — переживает падение
# любой отдельной ноды приложения.
# --------------------------------------------------------------------------- #
@app.get("/visits")
async def visits(request: Request) -> JSONResponse:
    """
    Глобальный счётчик запросов, общий для ВСЕХ нод за балансировщиком.
    Наглядно показывает, что состояние не привязано к конкретному backend'у:
    сколько бы нод ни стояло за Nginx/Apache, счётчик будет одним и тем же.
    """
    try:
        total = await db_manager.record_visit(
            node=str(NODE_ID),
            client=request.client.host if request.client else "-",
            max_recent=20,
        )
    except StorageUnavailable as exc:
        return JSONResponse(
            status_code=503,
            content={
                "error": "storage_unavailable",
                "detail": "Не удалось обратиться к PostgreSQL (внешнему хранилищу).",
                "served_by_node": NODE_ID,
                "reason": str(exc),
            },
        )
    return JSONResponse(content={"total_visits": total, "served_by_node": NODE_ID})


@app.get("/visits/recent")
async def visits_recent() -> JSONResponse:
    try:
        items = await db_manager.recent_visits(limit=20)
    except StorageUnavailable as exc:
        return JSONResponse(
            status_code=503,
            content={"error": "storage_unavailable", "served_by_node": NODE_ID, "reason": str(exc)},
        )
    return JSONResponse(content={"served_by_node": NODE_ID, "recent_requests": items})


# --------------------------------------------------------------------------- #
# Сессии в PostgreSQL (cookie с ID сессии, данные — только во внешнем хранилище)
# --------------------------------------------------------------------------- #
@app.get("/session")
async def session_counter(request: Request, response: Response) -> JSONResponse:
    """
    Демонстрация "sticky-less" сессий: ID сессии живёт в cookie у клиента,
    а счётчик визитов этой сессии — в PostgreSQL с TTL. Благодаря этому запросы
    одной сессии можно раскидывать балансировщиком по разным нодам без
    привязки к конкретному backend'у (без sticky sessions) и без риска
    потери данных при падении ноды, которая сессию создала.
    """
    session_id = request.cookies.get(settings.session_cookie_name)
    if not session_id:
        session_id = uuid.uuid4().hex

    try:
        hits = await db_manager.session_hit(session_id, settings.session_ttl_seconds)
    except StorageUnavailable as exc:
        return JSONResponse(
            status_code=503,
            content={
                "error": "storage_unavailable",
                "detail": "Хранилище сессий (PostgreSQL) недоступно.",
                "served_by_node": NODE_ID,
                "reason": str(exc),
            },
        )

    result = JSONResponse(
        content={
            "session_id": session_id,
            "hits_in_session": hits,
            "served_by_node": NODE_ID,
            "ttl_seconds": settings.session_ttl_seconds,
        }
    )
    result.set_cookie(
        key=settings.session_cookie_name,
        value=session_id,
        max_age=settings.session_ttl_seconds,
        httponly=True,
        secure=settings.session_cookie_secure,
        samesite="lax",
    )
    return result


# --------------------------------------------------------------------------- #
# Вспомогательные эндпоинты для нагрузочного тестирования
# --------------------------------------------------------------------------- #
@app.get("/bench", response_class=PlainTextResponse)
async def bench() -> str:
    """Лёгкий эндпоинт без обращения к хранилищу — для ab/wrk/hey и т.п."""
    return "1"


@app.get("/delay")
async def delay(ms: int = 200) -> dict:
    """
    Имитация асинхронной I/O-задержки (например, обращение к внешнему API/БД).
    Благодаря async/await и asyncio.sleep один процесс FastAPI продолжает
    обрабатывать другие запросы, пока эта "задержка" висит — полезно для
    демонстрации асинхронной обработки запросов под балансировщиком.
    """
    ms = max(0, min(ms, 10_000))
    started = time.monotonic()
    await asyncio.sleep(ms / 1000)
    return {
        "served_by_node": NODE_ID,
        "requested_delay_ms": ms,
        "actual_delay_ms": round((time.monotonic() - started) * 1000, 1),
    }


@app.get("/version")
async def version() -> dict:
    return {
        "app": "whoami-fastapi",
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "served_by_node": NODE_ID,
        "started_at_utc": START_TIME_ISO,
    }
