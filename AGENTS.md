# AGENTS.md

FastAPI clone of [traefik/whoami](https://github.com/traefik/whoami). Every response
identifies the backend node that produced it. It is a teaching artifact for a
reverse-proxy lab: 2+ identical instances run behind Apache for L4/L7
load balancing, DNS balancing, TLS termination and async request handling.
The goal is infrastructure artifacts, not a rich app — keep the code small.

## Stack

- Python 3.12 (`python:3.12-slim`; code needs >= 3.10 for `X | None`)
- FastAPI 0.115.0, uvicorn[standard] 0.30.6, asyncpg 0.30.0,
  pydantic 2.9.2 (pinned, not used directly)
- PostgreSQL 16 (`postgres:16-alpine`, named volume) as the only shared state
- Reverse proxy: Apache httpd, outside this repo (no config file and no service in compose)

## Commands

```bash
# setup (Windows: .venv\Scripts\activate; PowerShell env: $env:NODE_NAME="dev")
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# PostgreSQL for local runs (the app creates its own tables)
docker run -d --name whoami-pg-dev -p 5432:5432 -e POSTGRES_DB=whoami \
  -e POSTGRES_USER=whoami -e POSTGRES_PASSWORD=whoami postgres:16-alpine
export POSTGRES_HOST=localhost POSTGRES_PASSWORD=whoami

# one node with reload
NODE_NAME=dev uvicorn app.main:app --reload --port 8000

# two nodes sharing one PostgreSQL (no Docker needed for the app)
NODE_NAME=node-1 uvicorn app.main:app --port 8001
NODE_NAME=node-2 uvicorn app.main:app --port 8002

# full stack: postgres + app1 (:8001) + app2 (:8002). There is NO proxy service.
docker compose up --build -d
docker compose logs -f app1 app2
docker compose down          # keeps PostgreSQL data; add -v to wipe it

# smoke test (run after any behavior change)
curl -s localhost:8001/visits; curl -s localhost:8002/visits   # one shared, growing counter
curl -s -o /dev/null -D - localhost:8001/bench | grep -i '^x-node'   # 3 X-Node-* headers
curl -s localhost:8001/api                                     # node identity + echoed headers

# tests: none committed yet — see docs/agents/testing.md
python -m pytest -q
```

Expected with PostgreSQL stopped: `/`, `/api`, `/health` -> 200; `/health/ready`,
`/visits`, `/session` -> 503. More scenarios: `docs/agents/operations.md`.

## Boundaries

**Always**
- Keep state shared between requests in PostgreSQL, accessed only through
  `app/db_client.py` (`DatabaseManager`; handlers never touch connections or SQL).
  Database and socket errors become `StorageUnavailable` there.
- Turn `StorageUnavailable` into a `503` JSON response (shape below), never a 500.
- Put `served_by_node` in the JSON of every endpoint that does work. The
  `X-Node-Name/Hostname/Pid` middleware stays on all responses.
- Write handlers as `async def` with non-blocking calls only.
- Read configuration through `Settings` in `app/config.py` (env vars only).
- Update `README.md` and these docs in the same change when endpoints, env vars,
  ports, commands, tables or columns change.
- Keep each file's existing line endings (the whole repo is CRLF).
- Say explicitly what you could not run (Docker, PostgreSQL, Apache).

**Ask first**
- Changing a public contract: paths, JSON field names, `X-Node-*` headers, the 503
  shape, or existing table/column names and formats.
- Adding dependencies, a second datastore, auth or rate limiting.
- Changing the schema of an existing table: the app only runs `CREATE TABLE IF NOT EXISTS`,
  so a live database keeps the old definition and there are no migrations.
- Changing ports, service/container names or the compose network.
- Reformatting files or converting line endings.
- Fixing anything from `docs/agents/known-issues.md` that you were not asked to fix.

**Never**
- Keep shared data in process memory or local files (counters, caches, sessions).
  Per-node identity values (`NODE_ID`, `PID`, `START_TIME`) are fine.
- Handle TLS in the app: no `--ssl-*` flags, certificates, keys or HTTPS redirects.
  TLS terminates on Apache.
- Store sessions anywhere but PostgreSQL (no in-memory, file or client-side cookie sessions).
- Open database connections or pools outside `db_client.py`, build SQL with f-strings,
  or block the event loop (`time.sleep`, `requests`, sync drivers such as `psycopg2`).
- Add `--workers` to uvicorn, sticky sessions, or per-node logic like `if NODE_NAME == ...`.
- Use `/visits`, `/session` or `/health/ready` as a load-balancer health check
  (they mutate state or depend on PostgreSQL). Use `/health`.
- Commit secrets, certificates, keys, `.env` or `__pycache__`.

## Code conventions

No formatter or linter is configured. Match the surrounding code; do not reformat
unrelated lines. Type-hint everything. Comments and docstrings are in Russian
(match that); identifiers, JSON keys, table and column names are English. Tables use
the `whoami_` prefix. SQL lives in module-level constants in `db_client.py` and takes
values as `$n` parameters.

A data endpoint looks like this:

```python
@app.get("/example")
async def example() -> JSONResponse:
    try:
        value = await db_manager.incr_counter("example")  # a new DatabaseManager method
    except StorageUnavailable as exc:
        return JSONResponse(
            status_code=503,
            content={
                "error": "storage_unavailable",
                "detail": "Хранилище (PostgreSQL) недоступно.",
                "served_by_node": NODE_ID,
                "reason": str(exc),
            },
        )
    return JSONResponse(content={"value": value, "served_by_node": NODE_ID})
```

Do not write `except Exception: pass` around database calls, and do not import
`asyncpg` or call `asyncpg.connect(...)` from `main.py`.

## Where things live

- `app/main.py` — all endpoints, node identity (`NODE_ID`), `X-Node-*` middleware, `lifespan` (session purge task, pool shutdown)
- `app/db_client.py` — `DatabaseManager` (lazy asyncpg pool, 1 s timeouts, schema creation, all SQL), `StorageUnavailable`
- `app/config.py` — `Settings` dataclass

## Gotchas (not discoverable from the code alone)

- `Settings` reads env vars at import time (`os.getenv` in dataclass defaults).
  Changing `os.environ` afterwards does nothing; in tests patch objects instead.
- `.env` is never loaded: no dotenv, no `env_file`. `.env.example` is reference only.
  It is CRLF, so `source .env` leaves `\r` in values: `sed -i 's/\r$//' .env`.
- `PORT` in `Settings` is unused; the port comes from the Dockerfile `CMD`.
- `docker-compose.yml` has no proxy service. Nodes are published directly on `8001` and
  `8002`. `app1:8000`/`app2:8000` resolve only inside the compose network; for Apache on
  the host use `127.0.0.1:8001` and `:8002`.
- `HEAD` returns 405 on every route; checks that use `curl -I` will fail.
- `request.client` is the proxy address. The real client is in
  `X-Forwarded-For`; uvicorn trusts forwarded headers only from `127.0.0.1` by default.
- `container_id` is often `null` on cgroup v2; identity falls back to the hostname.
- The compose healthcheck uses `python -c urllib...` because the slim image has no `curl`.
- `GET /visits` increments the counter on every call, including prefetch and scanners.
- The schema is created by the app on the first successful connection (advisory lock,
  `IF NOT EXISTS`), so a fresh database needs no init script. The database user needs
  `CREATE` on it. PostgreSQL has no TTL: sessions expire via `expires_at` and a per-node
  purge task (`lifespan`).
- The `/health/ready` JSON field is `postgres` (it was `redis` before the migration).

## Git

No convention exists in the repo. Use short imperative subjects, one logical change
per commit, and mention in the body which boundary above the change touches.

## Deeper docs (read only when relevant)

- `docs/agents/reference.md` — endpoints, response contracts, database schema, env vars
- `docs/agents/operations.md` — compose, failure scenarios, PostgreSQL inspection
- `docs/agents/recipes.md` — new endpoint/env var/node, TLS and balancing on Apache, DNS round-robin
- `docs/agents/testing.md` — verified pytest setup against a real PostgreSQL and its pitfalls
- `docs/agents/known-issues.md` — documented problems and doc/config drift (K1..K25)
