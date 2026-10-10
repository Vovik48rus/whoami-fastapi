# Known issues

Found by reading the code and running parts of it. Do not fix these on the side. If
your task touches one, account for it and update its entry.

## Docs and config drift

K1-K4 (README/compose drift around a proxy service that does not exist) were removed; the IDs are not reused.

| ID | Problem | Fix |
|----|---------|-----|
| K5 | `Settings.port` (`PORT`) is never used; the port is in the Dockerfile `CMD` | Remove it or wire it to the `CMD` |
| K6 | `.env` is never loaded by the app (`.env.example` is reference only and now lists every variable, including the PostgreSQL timeouts). `docker compose` reads a project `.env` only for `${...}` substitution such as `POSTGRES_PASSWORD` | Document manual export |
| K7 | `version: "3.9"` in compose is obsolete; Compose v2 warns | Harmless; the line can go |

## Functional

| ID | Problem | Notes |
|----|---------|-------|
| K8 | `HEAD` returns 405 on all routes | Register `@app.api_route(path, methods=["GET", "HEAD"])` if a balancer probes with HEAD |
| K9 | `request.client.host` is the proxy address; uvicorn trusts `X-Forwarded-*` only from `127.0.0.1` by default | Add `--proxy-headers --forwarded-allow-ips=<proxy ip/network>` to `CMD`. A `*` value is risky because ports 8001/8002 are published, so direct clients could spoof the header |
| K10 | `get_container_id()` parses `/proc/self/cgroup`; on cgroup v2 it usually returns `None` | Do not rely on `container_id`; identity uses `NODE_NAME`/hostname (Docker's default hostname is the short container ID) |
| K11 | `get_local_ips()` calls blocking `socket.getaddrinfo` on every `/`, `/api`, `/ui` request inside `async def` | Compute once at startup; IPs are node identity, not shared state |
| K12 | `/visits` is a `GET` with side effects (the counter and log writes are now one transaction, so the old non-atomicity is gone) | Counter grows from prefetchers and scanners; never use as a health check |
| K13 | ~~No `lifespan` hook; the pool is never closed~~ Fixed by the PostgreSQL migration: `lifespan` stops the purge task and closes the pool | Keep it when editing `main.py` |
| K14 | Dead code: unused `BaseModel` import, unused `response: Response` parameter in `session_counter`, pinned but unused `pydantic` (the unused Redis `get`/`setex` methods disappeared with `redis_client.py`) | Remove only when asked |
| K15 | The 503 body of `/visits/recent` has no `detail` field | Align with `reference.md` |
| K16 | `/ui` builds HTML with f-strings without escaping | Values come from env/system today; anything from the request must go through `html.escape` |

## Security

| ID | Problem | Notes |
|----|---------|-------|
| K17 | The session cookie value is not validated; a client can send any string and it becomes a `whoami_sessions.session_id` row, living for the TTL (1 h) | Accept only `^[0-9a-f]{32}$`, otherwise issue a new ID. Risk: row growth. PostgreSQL makes it slightly worse than Redis: a value with a NUL byte (reproduced with `Cookie: whoami_session="\000"`) makes the query fail and the client gets a misleading `503 storage_unavailable`; a long, incompressible value can exceed the b-tree index row limit (about 2.7 kB) and fail the same way |
| K18 | `/` and `/api` echo all request headers (including `Cookie` and `Authorization`), IPs, PID, container ID; 503 `reason` can expose internal hosts | Acceptable for a lab (same as upstream whoami). Do not expose to untrusted networks or send secrets in headers |
| K19 | Nodes are published on host ports 8001/8002, bypassing the balancer and TLS | Handy for debugging; remove `ports:` for any shared deployment |
| K20 | No auth or rate limiting; `/delay` holds a request up to 10 s and `/visits` writes to PostgreSQL | Out of scope for the lab |
| K21 | The compose PostgreSQL password defaults to `whoami` (`${POSTGRES_PASSWORD:-whoami}`) and the app falls back to no password when `POSTGRES_PASSWORD` is unset; `/docs` is public | Protection is docker-network isolation. The PostgreSQL port is not published; set a real password for any shared deployment |

## Repository hygiene

| ID | Problem |
|----|---------|
| K22 | No tests, CI, linter/formatter or `.dockerignore` (the build context includes `.git`/`.venv`, but only `pyproject.toml`, `uv.lock`, `.python-version` and `app/` reach the image). The lock file now exists: `uv.lock` |
| K23 | All files are CRLF. Keep each file's endings; shell scripts, if ever added, must be LF; a `.env` copied from `.env.example` inherits CRLF |
| K24 | The Dockerfile has no `HEALTHCHECK` (compose defines one) |
| K25 | ~~Transitive dependencies are not pinned~~ Fixed by the move to uv: `uv.lock` pins all 30 packages, and the image installs with `--locked` |

## PostgreSQL storage

| ID | Problem | Notes |
|----|---------|-------|
| K26 | No migrations: the app runs `Base.metadata.create_all`. It creates missing tables (verified, also in a live database) but never alters an existing one, so editing a model does not change a database that already has that table | Fine for a lab; for column changes use `docker compose down -v`, or add Alembic (a new dependency: ask first) |
| K27 | Every `/visits` call updates one counter row and holds its lock until commit, so concurrent calls queue across all nodes. Measured on one CPU with 20 parallel requests over two nodes: waits up to about 2 s; with `POSTGRES_COMMAND_TIMEOUT=1` roughly 2-4 % of requests got a false 503 (the previous raw-asyncpg client under the same load: about 0.4 %), with 3 s none in 1000 | The default is 3 s. By design for a shared counter; benchmark with `/bench`, not `/visits` |
| K28 | Not run in Docker: the real image build, the `postgres:16-alpine` healthcheck and the compose startup order. The Dockerfile steps were emulated in a clean directory (`pip install uv==0.12.24`, `uv sync --locked --no-dev`, app started from that environment); everything else ran on a local PostgreSQL 16 | Run the smoke test after `docker compose up --build -d` |
| K29 | `uv.lock` is LF (written by uv) while other repo files are CRLF; `pyproject.toml` is CRLF and uv accepts it (`uv lock --check` and `uv sync --locked` passed). `.python-version` must stay LF: some version managers read a trailing `\r` as part of the version | Do not convert these two files. With `core.autocrlf=true` on Windows git may rewrite them; commit them as LF |
