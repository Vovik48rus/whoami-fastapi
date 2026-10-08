# Reference

Facts you must keep consistent. The live endpoint list is also served at `/docs`
(Swagger) when the app runs, and summarized in `README.md`.

## Endpoints

All routes are `GET` only.

| Path | Response | DB | Side effects | Codes |
|------|----------|:--:|--------------|-------|
| `/` | text/plain whoami dump (hostname, node, PID, IPs, RemoteAddr, request line, all headers) | no | none | 200 |
| `/api` | JSON identity + request + `server_time_utc`, `uptime_seconds` | no | none | 200 |
| `/ui` | HTML table of node identity | no | none | 200 |
| `/health` | JSON liveness (`status`, `node_name`, `hostname`) | no | none | 200 |
| `/health/ready` | JSON readiness (`status`, `node_name`, `postgres`) | `SELECT 1` | none | 200 / 503 |
| `/visits` | JSON shared counter | one transaction: counter upsert, log insert, log trim | increments counter, logs request | 200 / 503 |
| `/visits/recent` | JSON last <= 20 log entries, newest first | `SELECT` | none | 200 / 503 |
| `/session` | JSON + `Set-Cookie` | single upsert statement | creates/extends session | 200 / 503 |
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
  "detail": "Не удалось обратиться к PostgreSQL (внешнему хранилищу).",
  "served_by_node": "node-1",
  "reason": "[Errno 111] Connection refused"
}
```

`detail` is currently missing from `/visits/recent` (see K15); new endpoints include it.
`reason` can leak internal host names or SQL error text; hide it for any public deployment.

## Database schema

Tables are created by the app itself (`CREATE TABLE IF NOT EXISTS` under an advisory
lock, on the first successful connection), so there are no migration files. Prefix every
table with `whoami_` and document new ones here. Give unbounded tables a TTL column or a
row cap. A change to an existing table is a contract change: ask first, because
`IF NOT EXISTS` will not alter a table that already exists in a live database.

| Table | Columns | Purpose | Retention |
|-------|---------|---------|-----------|
| `whoami_counters` | `name text PK`, `value bigint` | named counters; `global:visits` is behind `/visits` | none |
| `whoami_recent_requests` | `id bigserial PK`, `created_at timestamptz`, `node text`, `client text` | request log behind `/visits/recent`; served as `ISO-time node=<id> client=<ip>` | capped at 20 rows by a `DELETE` in the same transaction as the insert |
| `whoami_sessions` | `session_id text PK`, `hits bigint`, `expires_at timestamptz` (index on `expires_at`) | hits in a session | sliding: every hit sets `expires_at = now() + SESSION_TTL_SECONDS`; expired rows are deleted by a background task |

Session cookie: `httponly`, `samesite=lax`, `secure` = `SESSION_COOKIE_SECURE`,
`max_age` = `SESSION_TTL_SECONDS`. The value is `uuid4().hex` unless the client sent
one. A client-supplied value is not validated (K17).

Expiry has no database TTL. A row past `expires_at` that the purge task has not removed
yet is treated as absent: the next hit resets `hits` to 1. The purge task runs on every
node every `SESSION_PURGE_INTERVAL_SECONDS`; the `DELETE` is idempotent, so running it
on several nodes is safe.

## Database layer (`app/db_client.py`)

`DatabaseManager` keeps a lazily created `asyncpg` pool (min 1, max
`POSTGRES_POOL_MAX`) and hands out connections through the private `_conn()` context
manager. Public methods are domain-level, each one a complete unit of work:
`ping`, `record_visit`, `recent_visits`, `session_hit`, `purge_expired_sessions`,
`close`. Handlers never see a connection or write SQL; `main.py` must not import `asyncpg`.

Rules for new methods:

1. Get the connection only through `async with self._conn() as conn:`. It converts
   `asyncpg.PostgresError`, `asyncpg.InterfaceError`, `OSError` and `TimeoutError` into
   `StorageUnavailable`, including errors raised by the query inside the block.
   `OSError` matters: socket and DNS failures are not `PostgresError`.
2. Use parameters (`$1`, `$2`), never f-strings, for any value; keep SQL in module
   constants next to the others.
3. Wrap statements that must succeed or fail together in `async with conn.transaction():`.
4. Do the whole read-modify-write in SQL (`INSERT ... ON CONFLICT DO UPDATE ... RETURNING`),
   not as read-then-write in Python: several nodes run it at once.
5. `ping()` is the exception: it returns a `bool` and never raises.

```python
async def get_counter(self, name: str) -> int:
    async with self._conn() as conn:
        value = await conn.fetchval("SELECT value FROM whoami_counters WHERE name = $1", name)
    return int(value or 0)
```

Behavior worth knowing:

- The pool is created on first use, not at startup. If PostgreSQL is down at boot the
  app still starts, every data call returns 503, and the first call after the database
  returns creates the pool and the schema. Nothing needs a restart (verified).
- `POSTGRES_CONNECT_TIMEOUT` bounds connecting and waiting for a free pooled connection;
  `POSTGRES_COMMAND_TIMEOUT` bounds each query. Both default to 1 s.
- `lifespan` in `app/main.py` starts the purge task and, on shutdown, cancels it and
  closes the pool (`db_manager.close()`).
- The pool is a module-level singleton bound to one event loop.
- `record_visit` is atomic, but every `/visits` call locks the same counter row until
  commit, so `/visits` throughput is serialized by design (fine for a lab; do not use it
  as a benchmark target, use `/bench`).

## Environment variables

| Variable | Default | Notes |
|----------|---------|-------|
| `NODE_NAME` | `""` | falls back to container ID, then hostname |
| `PORT` | `8000` | unused; the Dockerfile `CMD` sets the port |
| `POSTGRES_HOST` | `postgres` | compose service name; use `localhost` outside compose |
| `POSTGRES_PORT` | `5432` | |
| `POSTGRES_DB` | `whoami` | |
| `POSTGRES_USER` | `whoami` | needs `CREATE` on the database (the app creates its tables) |
| `POSTGRES_PASSWORD` | none | empty means no password; compose passes `${POSTGRES_PASSWORD:-whoami}` |
| `POSTGRES_CONNECT_TIMEOUT` | `1.0` s | connect and pool-acquire timeout; short on purpose so failures return 503 fast; may cause false 503s on a slow database |
| `POSTGRES_COMMAND_TIMEOUT` | `1.0` s | per-query timeout |
| `POSTGRES_POOL_MAX` | `10` | per node; keep `nodes * POSTGRES_POOL_MAX` below PostgreSQL `max_connections` (default 100) |
| `SESSION_TTL_SECONDS` | `3600` | `expires_at` offset and cookie `max_age`; sliding |
| `SESSION_PURGE_INTERVAL_SECONDS` | `60` | how often each node deletes expired sessions |
| `SESSION_COOKIE_NAME` | `whoami_session` | |
| `SESSION_COOKIE_SECURE` | `false` | accepts `1/true/yes/on`; enable only if every client uses HTTPS |

`.env.example` lists all of them. `POSTGRES_PASSWORD` is also read by `docker compose`
itself for variable substitution (from the shell or a project-level `.env`), unrelated
to the app never loading `.env`.
