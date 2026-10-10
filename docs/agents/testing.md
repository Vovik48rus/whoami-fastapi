# Testing

## Current state

The repo has no automated tests, CI, linter, formatter or `.dockerignore`.
Verification is manual (smoke test in `AGENTS.md`, scenarios in `operations.md`).
Do not claim tests passed unless you added and ran them.

## Verified setup (pytest + a real PostgreSQL)

There is no in-memory fake for PostgreSQL that is worth trusting: the SQL (`ON CONFLICT`,
`make_interval`, the trimming `DELETE`) must run on the real engine. The tests therefore
need a reachable PostgreSQL and read the same `POSTGRES_*` variables as the app. This
setup was run against the current code on PostgreSQL 16: 7 tests passed. Without a
reachable database the tests that need it are **skipped, which is not a pass**; read
the `-rs` output.

Dev dependencies (`pytest`, `httpx`) live in the `dev` dependency group of
`pyproject.toml`. `uv sync` installs it by default; the Docker image uses
`uv sync --locked --no-dev`, so it never reaches the image. `asyncpg` (the driver) is
a runtime dependency, so the test file can use it directly for setup.

Start a throwaway database and export the connection variables before pytest starts
(`Settings` reads them at import time):

```bash
docker run -d --name whoami-pg-test -p 5432:5432 \
  -e POSTGRES_DB=whoami -e POSTGRES_USER=whoami -e POSTGRES_PASSWORD=whoami postgres:16-alpine
export POSTGRES_HOST=localhost POSTGRES_PASSWORD=whoami
uv run pytest -q -rs
```

The fixture drops the three `whoami_*` tables before every test, so **never point it at
a database whose data you need**. Dropping them also checks that the app creates its own
schema.

`tests/test_smoke.py`:

```python
import asyncio

import asyncpg
import pytest
from fastapi.testclient import TestClient

from app import main
from app.config import settings
from app.db_client import db_manager
from app.main import app

TABLES = "whoami_counters, whoami_recent_requests, whoami_sessions"


def _run_sql(sql: str) -> None:
    async def go() -> None:
        conn = await asyncpg.connect(
            host=settings.postgres_host,
            port=settings.postgres_port,
            database=settings.postgres_db,
            user=settings.postgres_user,
            password=settings.postgres_password,
            timeout=2,
        )
        try:
            await conn.execute(sql)
        finally:
            await conn.close()

    asyncio.run(go())


@pytest.fixture()
def client():
    try:
        # Чистое состояние; заодно проверяем, что приложение само создаёт схему.
        _run_sql(f"DROP TABLE IF EXISTS {TABLES}")
    except (OSError, asyncpg.PostgresError, asyncio.TimeoutError) as exc:
        pytest.skip(f"PostgreSQL недоступен: {exc!r}")
    with TestClient(app) as c:
        yield c


def test_node_headers_on_every_response(client):
    r = client.get("/bench")
    assert r.status_code == 200
    assert r.headers["x-node-name"]
    assert r.headers["x-node-pid"]


def test_visits_counter_increments(client):
    a = client.get("/visits").json()["total_visits"]
    b = client.get("/visits").json()["total_visits"]
    assert b == a + 1


def test_recent_is_newest_first_and_capped(client):
    for _ in range(25):
        assert client.get("/visits").status_code == 200
    items = client.get("/visits/recent").json()["recent_requests"]
    assert len(items) == 20
    assert items == sorted(items, reverse=True)


def test_session_persists_via_cookie(client):
    first = client.get("/session").json()
    second = client.get("/session").json()
    assert first["session_id"] == second["session_id"]
    assert second["hits_in_session"] == first["hits_in_session"] + 1


def test_expired_session_restarts_from_one(client):
    first = client.get("/session").json()
    _run_sql("UPDATE whoami_sessions SET expires_at = now() - interval '1 second'")
    again = client.get("/session").json()
    assert again["session_id"] == first["session_id"]
    assert again["hits_in_session"] == 1


def test_two_nodes_share_state(client, monkeypatch):
    monkeypatch.setattr(main, "NODE_ID", "node-1")
    a = client.get("/visits").json()
    monkeypatch.setattr(main, "NODE_ID", "node-2")
    b = client.get("/visits").json()
    assert (a["served_by_node"], b["served_by_node"]) == ("node-1", "node-2")
    assert b["total_visits"] == a["total_visits"] + 1


def test_storage_down_gives_503_but_app_alive(monkeypatch):
    async def boom():
        raise OSError("db down")

    monkeypatch.setattr(db_manager, "_get_sessionmaker", boom)
    with TestClient(app) as c:
        assert c.get("/health").status_code == 200
        assert c.get("/api").status_code == 200
        assert c.get("/health/ready").status_code == 503
        r = c.get("/visits")
        assert r.status_code == 503
        assert r.json()["error"] == "storage_unavailable"
        assert c.get("/session").status_code == 503
```

Run from the repo root: `uv run pytest -q -rs`. The last test needs no database
and runs even when the others are skipped.

## Pitfalls

- To simulate "database down", patch `db_manager._get_sessionmaker` (it is called inside
  the `try`, so any `OSError` becomes `StorageUnavailable`). Do not build a second engine.
- The engine's connection pool is bound to the event loop that created it.
  `with TestClient(app)` runs one loop for the whole block and the `lifespan` shutdown
  disposes the engine, so use the `with` form every time; a client created without it
  would reuse connections from a dead loop.
- `lifespan` also starts the session-purge task. It sleeps
  `SESSION_PURGE_INTERVAL_SECONDS` (60 s) before the first purge, so it does not
  interfere with tests; to test purging, call `await db_manager.purge_expired_sessions()`.
- `Settings` reads env vars at import time. `monkeypatch.setenv` after
  `import app.config` has no effect: export variables before pytest starts, or patch
  `app.main.NODE_ID` and similar objects.
- To simulate two nodes, patch `app.main.NODE_ID` to `"node-1"` and `"node-2"` in
  turn (both "nodes" use the same database, which is exactly the shared state).
- `TestClient` sends no proxy headers; pass them yourself
  (`headers={"X-Forwarded-For": "203.0.113.5"}`).
- Tests do not cover compose DNS, Nginx `max_fails`, or container stop/start. Those need Docker.
- Do not load-test `/visits` in a test: every call updates one counter row, so
  concurrent calls queue up (K27). A burst of 20 parallel requests per node on a single
  CPU needed `POSTGRES_COMMAND_TIMEOUT` above 2 s to avoid occasional 503s.

## What to cover when changing code

Node headers and `served_by_node` on new endpoints, 503 behavior when the database is
down, the shared counter, session continuity across simulated nodes and session
expiry, the `/delay` clamp, and the 503 body shape.
