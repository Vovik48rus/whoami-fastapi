# AGENTS.md

FastAPI clone of [traefik/whoami](https://github.com/traefik/whoami). Every response
identifies the backend node that produced it. It is a teaching artifact for a
reverse-proxy lab: 2+ identical instances run behind Nginx/Apache for L4/L7
load balancing, DNS balancing, TLS termination and async request handling.
The goal is infrastructure artifacts, not a rich app — keep the code small.

## Stack

- Python 3.12 (`python:3.12-slim`; code needs >= 3.10 for `X | None`)
- FastAPI 0.115.0, uvicorn[standard] 0.30.6, redis-py 5.0.8 (`redis.asyncio`),
  pydantic 2.9.2 (pinned, not used directly)
- Redis 7 (`redis:7-alpine`, AOF on) as the only shared state
- Nginx 1.27 as the reverse proxy (`nginx.conf`; not a service in compose)

## Commands

```bash
# setup (Windows: .venv\Scripts\activate; PowerShell env: $env:NODE_NAME="dev")
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# Redis for local runs
docker run -d --name whoami-redis-dev -p 6379:6379 redis:7-alpine

# one node with reload
NODE_NAME=dev REDIS_HOST=localhost uvicorn app.main:app --reload --port 8000

# two nodes sharing one Redis (no Docker needed for the app)
NODE_NAME=node-1 REDIS_HOST=localhost uvicorn app.main:app --port 8001
NODE_NAME=node-2 REDIS_HOST=localhost uvicorn app.main:app --port 8002

# full stack: redis + app1 (:8001) + app2 (:8002). There is NO nginx service.
docker compose up --build -d
docker compose logs -f app1 app2
docker compose down          # keeps Redis data; add -v to wipe it

# smoke test (run after any behavior change)
curl -s localhost:8001/visits; curl -s localhost:8002/visits   # one shared, growing counter
curl -s -o /dev/null -D - localhost:8001/bench | grep -i '^x-node'   # 3 X-Node-* headers
curl -s localhost:8001/api                                     # node identity + echoed headers

# tests: none committed yet — see docs/agents/testing.md
python -m pytest -q
```

Expected with Redis stopped: `/`, `/api`, `/health` -> 200; `/health/ready`,
`/visits`, `/session` -> 503. More scenarios: `docs/agents/operations.md`.

## Boundaries

**Always**
- Keep state shared between requests in Redis, accessed only through
  `app/redis_client.py`. Wrap `RedisError` and `OSError` into `RedisUnavailable`.
- Turn `RedisUnavailable` into a `503` JSON response (shape below), never a 500.
- Put `served_by_node` in the JSON of every endpoint that does work. The
  `X-Node-Name/Hostname/Pid` middleware stays on all responses.
- Write handlers as `async def` with non-blocking calls only.
- Read configuration through `Settings` in `app/config.py` (env vars only).
- Update `README.md` and these docs in the same change when endpoints, env vars,
  ports, commands or Redis keys change.
- Keep each file's existing line endings (the whole repo is CRLF).
- Say explicitly what you could not run (Docker, Redis, Nginx).

**Ask first**
- Changing a public contract: paths, JSON field names, `X-Node-*` headers, the 503
  shape, or existing Redis key names/formats.
- Adding dependencies, a second datastore, auth or rate limiting.
- Changing ports, service/container names or the compose network.
- Reformatting files or converting line endings.
- Fixing anything from `docs/agents/known-issues.md` that you were not asked to fix.

**Never**
- Keep shared data in process memory or local files (counters, caches, sessions).
  Per-node identity values (`NODE_ID`, `PID`, `START_TIME`) are fine.
- Handle TLS in the app: no `--ssl-*` flags, certificates, keys or HTTPS redirects.
  TLS terminates on Nginx/Apache.
- Store sessions anywhere but Redis (no in-memory, file or client-side cookie sessions).
- Create Redis clients outside `redis_client.py`, or block the event loop
  (`time.sleep`, `requests`, sync `redis`).
- Add `--workers` to uvicorn, sticky sessions, or per-node logic like `if NODE_NAME == ...`.
- Use `/visits`, `/session` or `/health/ready` as a load-balancer health check
  (they mutate state or depend on Redis). Use `/health`.
- Commit secrets, certificates, keys, `.env` or `__pycache__`.

## Code conventions

No formatter or linter is configured. Match the surrounding code; do not reformat
unrelated lines. Type-hint everything. Comments and docstrings are in Russian
(match that); identifiers, JSON keys and Redis keys are English. Redis keys use the
`whoami:<area>:<name>` prefix.

A data endpoint looks like this:

```python
@app.get("/example")
async def example() -> JSONResponse:
    try:
        value = await redis_manager.incr("whoami:example:counter")
    except RedisUnavailable as exc:
        return JSONResponse(
            status_code=503,
            content={
                "error": "storage_unavailable",
                "detail": "Хранилище (Redis) недоступно.",
                "served_by_node": NODE_ID,
                "reason": str(exc),
            },
        )
    return JSONResponse(content={"value": value, "served_by_node": NODE_ID})
```

Do not write `except Exception: pass` around Redis, and do not call
`redis.Redis(...)` from `main.py`.

## Where things live

- `app/main.py` — all endpoints, node identity (`NODE_ID`), `X-Node-*` middleware
- `app/redis_client.py` — `RedisManager` (lazy pool, 0.5 s timeouts), `RedisUnavailable`
- `app/config.py` — `Settings` dataclass
- `nginx.conf` — upstream + server fragment for `conf.d/`, not a full nginx config

## Gotchas (not discoverable from the code alone)

- `Settings` reads env vars at import time (`os.getenv` in dataclass defaults).
  Changing `os.environ` afterwards does nothing; in tests patch objects instead.
- `.env` is never loaded: no dotenv, no `env_file`. `.env.example` is reference only.
  It is CRLF, so `source .env` leaves `\r` in values: `sed -i 's/\r$//' .env`.
- `PORT` in `Settings` is unused; the port comes from the Dockerfile `CMD`.
- `README.md` describes an Nginx balancer on `:8080`, but `docker-compose.yml` has no
  nginx service. Nodes are published directly on `8001` and `8002`.
- `nginx.conf` upstream `app1:8000`/`app2:8000` resolves only inside the compose
  network. For Nginx/Apache on the host use `127.0.0.1:8001` and `:8002`.
- `HEAD` returns 405 on every route; checks that use `curl -I` will fail.
- `request.client` is the proxy address. The real client is in `X-Real-IP` /
  `X-Forwarded-For`; uvicorn trusts forwarded headers only from `127.0.0.1` by default.
- `container_id` is often `null` on cgroup v2; identity falls back to the hostname.
- The compose healthcheck uses `python -c urllib...` because the slim image has no `curl`.
- `GET /visits` increments the counter on every call, including prefetch and scanners.

## Git

No convention exists in the repo. Use short imperative subjects, one logical change
per commit, and mention in the body which boundary above the change touches.

## Deeper docs (read only when relevant)

- `docs/agents/reference.md` — endpoints, response contracts, Redis keys, env vars
- `docs/agents/operations.md` — compose, failure scenarios, Redis inspection, Nginx pitfalls
- `docs/agents/recipes.md` — new endpoint/env var/node, TLS, L4 stream, Apache, DNS round-robin
- `docs/agents/testing.md` — verified pytest + fakeredis setup and its pitfalls
- `docs/agents/known-issues.md` — documented problems and doc/config drift (K1..K25)
