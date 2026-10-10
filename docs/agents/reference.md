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

The schema is defined once, as ORM models in `app/models.py` (SQLAlchemy 2.0 typed style,
declarative `Base`). The app creates the tables itself (`Base.metadata.create_all` under
an advisory lock, on the first successful connection), so there are no migration files.
Prefix every table with `whoami_`, add a model for each new one and document it here.
Give unbounded tables a TTL column or a row cap. A change to an existing model is a
contract change: ask first, because `create_all` never alters a table that already
exists in a live database.

| Table (model) | Columns | Purpose | Retention |
|---------------|---------|---------|-----------|
| `whoami_counters` (`Counter`) | `name text PK`, `value bigint` | named counters; `global:visits` is behind `/visits` | none |
| `whoami_recent_requests` (`RecentRequest`) | `id bigserial PK`, `created_at timestamptz`, `node text`, `client text` | request log behind `/visits/recent`; served as `ISO-time node=<id> client=<ip>` | capped at 20 rows by a `DELETE` in the same transaction as the insert |
| `whoami_sessions` (`SessionRow`) | `session_id text PK`, `hits bigint`, `expires_at timestamptz` (index `whoami_sessions_expires_idx`) | hits in a session | sliding: every hit sets `expires_at = now() + SESSION_TTL_SECONDS`; expired rows are deleted by a background task |

Session cookie: `httponly`, `samesite=lax`, `secure` = `SESSION_COOKIE_SECURE`,
`max_age` = `SESSION_TTL_SECONDS`. The value is `uuid4().hex` unless the client sent
one. A client-supplied value is not validated (K17).

Expiry has no database TTL. A row past `expires_at` that the purge task has not removed
yet is treated as absent: the next hit resets `hits` to 1. The purge task runs on every
node every `SESSION_PURGE_INTERVAL_SECONDS`; the `DELETE` is idempotent, so running it
on several nodes is safe.

## Database layer (`app/models.py`, `app/db_client.py`)

`DatabaseManager` creates a SQLAlchemy `AsyncEngine` lazily (driver `asyncpg`; URL built
with `URL.create`, so special characters in the password are safe) and hands out
`AsyncSession` objects through the private `_session()` context manager. Public
methods are domain-level, each one a complete unit of work in one transaction:
`ping`, `record_visit`, `recent_visits`, `session_hit`, `purge_expired_sessions`,
`close`. Handlers never see a session or write a query; `main.py` must not import
`sqlalchemy` or `asyncpg`.

Rules for new methods:

1. Get the session only through `async with self._session() as session:`. It opens a
   transaction (commit on exit, rollback on error) and converts `SQLAlchemyError`
   (driver errors, pool exhaustion), `OSError` and `TimeoutError` into
   `StorageUnavailable`, including errors raised by a query inside the block.
   `OSError` matters: SQLAlchemy does not wrap socket and DNS failures.
2. Write queries with the ORM/expression API, never with strings or f-strings; values
   are bound parameters. Put the model in `app/models.py` first.
3. Do the whole read-modify-write in one statement
   (`pg_insert(Model).on_conflict_do_update(...).returning(...)`), not as
   read-then-write in Python: several nodes run it at once. Use `session.scalar(...)`
   for one value, `session.scalars(...)` for rows.
4. Never return ORM objects from a method: sessions are closed on exit and the objects
   are not safe to use outside the transaction. Return plain values.
5. `ping()` is the exception: it returns a `bool` and never raises.

```python
async def get_counter(self, name: str) -> int:
    async with self._session() as session:
        value = await session.scalar(select(Counter.value).where(Counter.name == name))
    return int(value or 0)
```

Behavior worth knowing:

- The engine is created on first use, not at startup. If PostgreSQL is down at boot the
  app still starts, every data call returns 503, and the first call after the database
  returns creates the schema and works. Nothing needs a restart (verified).
- Pool: `pool_size = POSTGRES_POOL_MAX`, no overflow, `pool_pre_ping=True` (a dead
  connection after a PostgreSQL restart is replaced instead of failing a request; costs one
  extra round trip per checkout). `POSTGRES_CONNECT_TIMEOUT` bounds connecting and
  waiting for a free pooled connection; `POSTGRES_COMMAND_TIMEOUT` bounds each query.
- The `reason` in a 503 is the driver's message only. `str()` of a SQLAlchemy `DBAPIError`
  contains the SQL text and bound parameters (session ids), so `_reason()` unwraps `.orig`.
  Keep that when touching error handling.
- `lifespan` in `app/main.py` starts the purge task and, on shutdown, cancels it and
  disposes the engine (`db_manager.close()`).
- The engine is a module-level singleton and its pool is bound to one event loop.
- `record_visit` is atomic, but every `/visits` call locks the same counter row until
  commit, so concurrent calls queue (K27). With 20 parallel requests on a single CPU the
  queue reached about 2 s, which is why `POSTGRES_COMMAND_TIMEOUT` defaults to 3 s.
  Use `/bench`, not `/visits`, as a benchmark target.

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
| `POSTGRES_COMMAND_TIMEOUT` | `3.0` s | per-query timeout; also covers waiting on the counter row lock under load |
| `POSTGRES_POOL_MAX` | `10` | pool size per node, no overflow; keep `nodes * POSTGRES_POOL_MAX` below PostgreSQL `max_connections` (default 100) |
| `SESSION_TTL_SECONDS` | `3600` | `expires_at` offset and cookie `max_age`; sliding |
| `SESSION_PURGE_INTERVAL_SECONDS` | `60` | how often each node deletes expired sessions |
| `SESSION_COOKIE_NAME` | `whoami_session` | |
| `SESSION_COOKIE_SECURE` | `false` | accepts `1/true/yes/on`; enable only if every client uses HTTPS |

`.env.example` lists all of them. `POSTGRES_PASSWORD` is also read by `docker compose`
itself for variable substitution (from the shell or a project-level `.env`), unrelated
to the app never loading `.env`.
