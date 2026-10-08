# Known issues

Found by reading the code and running parts of it. Do not fix these on the side. If
your task touches one, account for it and update its entry.

## Docs and config drift

| ID | Problem | Fix |
|----|---------|-----|
| K1 | `README.md` describes "2 nodes + Redis + Nginx" on `:8080`, but `docker-compose.yml` has no nginx service; nodes are on `8001`/`8002`. README commands using `localhost:8080` fail out of the box | Restore the service (`operations.md`, option A) or fix the README |
| K2 | The header comment in `docker-compose.yml` mentions nginx, which is not there | Align with K1 |
| K3 | README suggests `docker compose up --scale app1=1 --scale app2=1`: a no-op, and real scaling is blocked by `container_name` and fixed `ports` | Use explicit services (`recipes.md`) |
| K4 | `nginx.conf` upstreams `app1:8000`/`app2:8000` resolve only inside the compose network | On a host use `127.0.0.1:8001` / `:8002` or real addresses |
| K5 | `Settings.port` (`PORT`) is never used; the port is in the Dockerfile `CMD` | Remove it or wire it to the `CMD` |
| K6 | `.env.example` lacks the Redis timeout variables; `.env` is never loaded | Complete the example; document manual export |
| K7 | `version: "3.9"` in compose is obsolete; Compose v2 warns | Harmless; the line can go |

## Functional

| ID | Problem | Notes |
|----|---------|-------|
| K8 | `HEAD` returns 405 on all routes | Register `@app.api_route(path, methods=["GET", "HEAD"])` if a balancer probes with HEAD |
| K9 | `request.client.host` is the proxy address; uvicorn trusts `X-Forwarded-*` only from `127.0.0.1` by default | Add `--proxy-headers --forwarded-allow-ips=<proxy ip/network>` to `CMD`. A `*` value is risky because ports 8001/8002 are published, so direct clients could spoof the header |
| K10 | `get_container_id()` parses `/proc/self/cgroup`; on cgroup v2 it usually returns `None` | Do not rely on `container_id`; identity uses `NODE_NAME`/hostname (Docker's default hostname is the short container ID) |
| K11 | `get_local_ips()` calls blocking `socket.getaddrinfo` on every `/`, `/api`, `/ui` request inside `async def` | Compute once at startup; IPs are node identity, not shared state |
| K12 | `/visits` is a `GET` with side effects; `INCR` and `LPUSH/LTRIM` are not atomic together | Counter grows from prefetchers and scanners; never use as a health check |
| K13 | No `lifespan` hook; the Redis pool is never closed | Add one if clean shutdown matters |
| K14 | Dead code: unused `BaseModel` import, unused `response: Response` parameter in `session_counter`, unused `RedisManager.get`/`setex`, pinned but unused `pydantic` | Remove only when asked |
| K15 | The 503 body of `/visits/recent` has no `detail` field | Align with `reference.md` |
| K16 | `/ui` builds HTML with f-strings without escaping | Values come from env/system today; anything from the request must go through `html.escape` |

## Security

| ID | Problem | Notes |
|----|---------|-------|
| K17 | The session cookie value is not validated; a client can send any string and it becomes part of the key `whoami:session:<value>`, living for the TTL (1 h) | Accept only `^[0-9a-f]{32}$`, otherwise issue a new ID. Risk: key-space pollution and Redis memory growth |
| K18 | `/` and `/api` echo all request headers (including `Cookie` and `Authorization`), IPs, PID, container ID; 503 `reason` can expose internal hosts | Acceptable for a lab (same as upstream whoami). Do not expose to untrusted networks or send secrets in headers |
| K19 | Nodes are published on host ports 8001/8002, bypassing the balancer and TLS | Handy for debugging; remove `ports:` for any shared deployment |
| K20 | No auth or rate limiting; `/delay` holds a request up to 10 s and `/visits` writes to Redis | Out of scope for the lab |
| K21 | Redis has no password (the app supports `REDIS_PASSWORD`, the container does not require it); `/docs` is public | Protection is docker-network isolation |

## Repository hygiene

| ID | Problem |
|----|---------|
| K22 | No tests, CI, linter/formatter, lock file or `.dockerignore` (the build context includes `.git`/`.venv`, but only `requirements.txt` and `app/` reach the image) |
| K23 | All files are CRLF. Keep each file's endings; shell scripts, if ever added, must be LF; a `.env` copied from `.env.example` inherits CRLF |
| K24 | The Dockerfile has no `HEALTHCHECK` (compose defines one) |
| K25 | Transitive dependencies are not pinned |
