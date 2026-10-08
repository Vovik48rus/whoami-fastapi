# Testing

## Current state

The repo has no automated tests, CI, linter, formatter, lock file or `.dockerignore`.
Verification is manual (smoke test in `AGENTS.md`, scenarios in `operations.md`).
Do not claim tests passed unless you added and ran them.

## Verified setup (pytest + fakeredis)

This setup was run against the current code: 4 tests passed. Keep dev dependencies out
of `requirements.txt` (it goes into the image); use a separate `requirements-dev.txt`:

```text
pytest
httpx
fakeredis
```

`tests/test_smoke.py`:

```python
import fakeredis.aioredis
import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.redis_client import redis_manager


@pytest.fixture()
def client(monkeypatch):
    fake = fakeredis.aioredis.FakeRedis(decode_responses=True)
    monkeypatch.setattr(redis_manager, "client", lambda: fake)
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


def test_session_persists_via_cookie(client):
    first = client.get("/session").json()
    second = client.get("/session").json()
    assert first["session_id"] == second["session_id"]
    assert second["hits_in_session"] == first["hits_in_session"] + 1


def test_storage_down_gives_503_but_app_alive(monkeypatch):
    def boom():
        raise OSError("redis down")
    monkeypatch.setattr(redis_manager, "client", boom)
    with TestClient(app) as c:
        assert c.get("/health").status_code == 200
        assert c.get("/visits").status_code == 503
```

Run from the repo root: `python -m pytest -q`.

## Pitfalls

- Replace `redis_manager.client`; do not build a second pool. The pool is a
  module-level singleton tied to one event loop.
- `Settings` reads env vars at import time. `monkeypatch.setenv` after
  `import app.config` has no effect: export variables before pytest starts, or patch
  `app.main.NODE_ID` and similar objects.
- To simulate two nodes, share one `FakeRedis` and patch `app.main.NODE_ID` to
  `"node-1"` and `"node-2"` in turn.
- `TestClient` sends no proxy headers; pass them yourself
  (`headers={"X-Forwarded-For": "203.0.113.5"}`).
- Tests do not cover compose DNS, Nginx `max_fails`, or container stop/start. Those need Docker.

## What to cover when changing code

Node headers and `served_by_node` on new endpoints, 503 behavior when Redis is down,
the shared counter, session continuity across simulated nodes, the `/delay` clamp, and
the 503 body shape.
