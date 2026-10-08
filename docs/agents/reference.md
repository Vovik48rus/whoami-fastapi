# Reference

Facts you must keep consistent. The live endpoint list is also served at `/docs`
(Swagger) when the app runs, and summarized in `README.md`.

## Endpoints

All routes are `GET` only.

| Path | Response | Redis | Side effects | Codes |
|------|----------|:-----:|--------------|-------|
| `/` | text/plain whoami dump (hostname, node, PID, IPs, RemoteAddr, request line, all headers) | no | none | 200 |
| `/api` | JSON identity + request + `server_time_utc`, `uptime_seconds` | no | none | 200 |
| `/ui` | HTML table of node identity | no | none | 200 |
| `/health` | JSON liveness (`status`, `node_name`, `hostname`) | no | none | 200 |
| `/health/ready` | JSON readiness | `PING` | none | 200 / 503 |
| `/visits` | JSON shared counter | `INCR`, `LPUSH`+`LTRIM` | increments counter, logs request | 200 / 503 |
| `/visits/recent` | JSON last <= 20 log entries | `LRANGE` | none | 200 / 503 |
| `/session` | JSON + `Set-Cookie` | `INCR`+`EXPIRE` (pipeline) | creates/extends session | 200 / 503 |
| `/bench` | text/plain `1` | no | none | 200 |
| `/delay?ms=N` | JSON; async sleep, `ms` clamped to 0..10000, default 200 | no | none | 200 |
| `/version` | JSON Python/platform/start time | no | none | 200 |

`/` formats header names with `name.title()` and mimics traefik/whoami; scripts may
parse it, so avoid changing its layout without a reason.

## Node identification contract

| Where | What |
|-------|------|
| Every response (middleware `add_node_headers`) | `X-Node-Name`, `X-Node-Hostname`, `X-Node-Pid` |
| Identity endpoints (`/api`, `/health`) | `node_name`, `hostname`, and in `/api` also `container_id`, `pid`, `ips` |
| Endpoints that do work or touch data | `served_by_node` |

`NODE_ID` = `NODE_NAME` env var, else container ID, else hostname. For unhandled
exceptions (500) the headers may be missing because the middleware does not catch them.

## Storage-failure response (503)

```json
{
  "error": "storage_unavailable",
  "detail": "Не удалось обратиться к Redis (внешнему хранилищу).",
  "served_by_node": "node-1",
  "reason": "Error 111 connecting to redis:6379. Connect call failed (...)"
}
```

`detail` is currently missing from `/visits/recent` (see K15); new endpoints include it.
`reason` can leak internal host names; hide it for any public deployment.

## Redis keys

Prefix everything with `whoami:`. Document new keys here. Give unbounded keys a TTL
or a length cap.

| Key | Type | Purpose | TTL |
|-----|------|---------|-----|
| `whoami:global:visits` | string (int) | shared counter behind `/visits` | none |
| `whoami:global:recent_requests` | list | last <= 20 entries `ISO-time node=<id> client=<ip>` | none (capped by `LTRIM`) |
| `whoami:session:<session_id>` | string (int) | hits in a session | `SESSION_TTL_SECONDS`, refreshed on each hit |

Session cookie: `httponly`, `samesite=lax`, `secure` = `SESSION_COOKIE_SECURE`,
`max_age` = `SESSION_TTL_SECONDS`. The value is `uuid4().hex` unless the client sent
one. A client-supplied value is not validated (K17).

## Redis layer (`app/redis_client.py`)

`RedisManager` keeps a lazy `ConnectionPool` (max 20, `decode_responses=True`) and
hands out `redis.asyncio.Redis` via `client()`. Methods: `ping`, `incr`, `get`,
`setex`, `incr_with_ttl`, `lpush_capped`, `lrange` (`get` and `setex` are unused).

Rules for new methods:

1. Wrap the call in `try/except (RedisError, OSError) as exc: raise RedisUnavailable(str(exc)) from exc`.
   `OSError` matters: socket and DNS failures do not always arrive as `RedisError`.
2. Call `self.client()` inside the `try`; tests replace it and the replacement may raise.
3. Use `pipeline(transaction=True)` for commands that must run together.
4. `ping()` is the exception: it returns a `bool` and never raises.

```python
async def hincr(self, key: str, field: str) -> int:
    try:
        return await self.client().hincrby(key, field, 1)
    except (RedisError, OSError) as exc:
        raise RedisUnavailable(str(exc)) from exc
```

Limits: there is no `lifespan` hook, so the pool is never closed; the pool is a
module-level singleton bound to one event loop; `/visits` does two separate Redis
calls, so a failure between them leaves the counter incremented but returns 503.

## Environment variables

| Variable | Default | Notes |
|----------|---------|-------|
| `NODE_NAME` | `""` | falls back to container ID, then hostname |
| `PORT` | `8000` | unused; the Dockerfile `CMD` sets the port |
| `REDIS_HOST` | `redis` | compose service name; use `localhost` outside compose |
| `REDIS_PORT` | `6379` | |
| `REDIS_DB` | `0` | |
| `REDIS_PASSWORD` | none | empty string means no password; the Redis container does not require one |
| `REDIS_CONNECT_TIMEOUT` | `0.5` s | short on purpose so failures return 503 fast; may cause false 503s on a slow Redis |
| `REDIS_SOCKET_TIMEOUT` | `0.5` s | same |
| `SESSION_TTL_SECONDS` | `3600` | key TTL and cookie `max_age`; sliding |
| `SESSION_COOKIE_NAME` | `whoami_session` | |
| `SESSION_COOKIE_SECURE` | `false` | accepts `1/true/yes/on`; enable only if every client uses HTTPS |

`.env.example` omits the two timeout variables; add them when you touch it.
