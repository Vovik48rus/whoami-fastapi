# Best practices (research digest)

Researched 2026-10-09 from public sources (list in section 14), then checked against this
repo's code. Read it when you design or change: storage code, health checks, uvicorn/
Docker settings, sessions, tests. Skip it for a one-line fix.

## 0. How to use this file

1. **Priority.** `AGENTS.md` boundaries beat this file. If a source says X and `AGENTS.md`
   says Y, do Y. Known conflicts are in section 10.
2. **Rule format.** Every rule has: ID, imperative **Rule**, **Why**, **Repo** status.
3. **Repo status values.**
   - `OK` — the repo already does it. Keep it.
   - `GAP` — the repo differs. This is NOT permission to fix. Fix only when asked, and if the
     fix touches an "Ask first" item in `AGENTS.md`, ask first. Update the `K` entry afterwards.
   - `INTENTIONAL` — the repo differs on purpose. Do not "fix" it.
4. **Verification tags** (how much to trust a rule):
   - `[V]` confirmed in a retrieved source (source id in brackets, section 14).
   - `[I]` inference: a sourced principle applied to this repo's code. Measure before claiming a gain.
   - `[G]` general engineering knowledge, not re-checked during this research.
5. Most sources were secondary (blogs, skill catalogs). Primary ones: uvicorn docs, FastAPI
   docs, OWASP, asyncpg repository. Weigh accordingly.

## 1. Mental model

```
client -> [Apache: TLS, balancing] -> N identical uvicorn nodes -> ONE PostgreSQL
                                             (1 process each, differ only by env)  (all shared state)
```

- A node is stateless and disposable. Identity (`NODE_ID`, `PID`) is the only per-node data.
- Anything that must survive a node or must be seen by another node lives in PostgreSQL.
- PostgreSQL down => data endpoints answer `503`; the node and `/`, `/api`, `/health` stay up.
- The repo is a lab artifact: prefer small and obvious over clever.

## 2. Core principles (the patterns behind the rules)

| ID | Principle | Meaning here | Where it shows |
|----|-----------|--------------|----------------|
| P1 | Stateless nodes, externalized state | no counters/sessions/caches in process memory or files | `db_client.py`, section 4 |
| P2 | Bounded waiting | every wait has a timeout; fail fast instead of hanging | `POSTGRES_*_TIMEOUT`, section 4, 6 |
| P3 | Graceful degradation | lose one dependency, lose only the endpoints that need it | lazy pool, 503 pattern |
| P4 | Translate errors at the boundary | driver exceptions never leave `db_client.py`; handlers see `StorageUnavailable` | `_conn()` |
| P5 | Atomic operations in SQL | read-modify-write happens in one statement/transaction, not in Python | upserts with `RETURNING` |
| P6 | Trust boundaries | only the proxy may set forwarded headers; cookie, header and query values are untrusted | section 6, 7 |
| P7 | Liveness is not readiness | restart-me and stop-sending-traffic are different signals | section 5 |
| P8 | Align timeouts across layers | proxy, app server and database timeouts must be ordered, not independent | section 6 |
| P9 | Docs are part of the change | contract, env var, table or command changes update README and `docs/agents/` | `AGENTS.md` Always |

## 3. FastAPI and asyncio

**BP-APP-01 — Never block the event loop.** [V S1]
Rule: inside `async def` use only awaitable I/O. No `time.sleep`, `requests`, `psycopg2`,
blocking DNS or file reads on the request path. CPU-heavy work goes to a process/thread pool.
Why: one blocked call stalls every request of that node, which looks like a node outage.
Repo: `GAP` for `get_local_ips()` (blocking `getaddrinfo` per request, K11). Fix by computing once
at startup; IPs are node identity, not shared state.

**BP-APP-02 — Use `lifespan`, not `@app.on_event`.** [V S1]
Rule: startup/shutdown logic is one `@asynccontextmanager lifespan`. `on_event` is deprecated.
Repo: `OK` (`lifespan` starts the purge task, cancels it, closes the pool).

**BP-APP-03 — Lazy, fail-soft initialization is a deliberate choice here.** [I]
Generic advice says lifespan should fail loudly when a mandatory dependency is missing [S1].
This repo chooses the opposite: the pool is created on first data call so the app starts while
PostgreSQL is down (P3).
Rule: do not add an eager `create_pool` or a "connect or crash" check to `lifespan`.
Repo: `INTENTIONAL`.

**BP-APP-04 — One uvicorn process per container; scale by adding containers.** [V S1]
Why: `lifespan` runs once per worker, so each worker owns its own pool. Real pool usage is
`workers x nodes x POSTGRES_POOL_MAX`. Separate containers are also what the lab demonstrates.
Repo: `OK` (`--workers` is forbidden in `AGENTS.md`).
Note: uvicorn's deployment page recommends gunicorn workers for production [S3]. That is a
conflict with this lab's goal, not a TODO.

**BP-APP-05 — One place for repeated error translation.** [V S1 shows handler pattern] [I]
Option: a single `@app.exception_handler(StorageUnavailable)` returning the 503 JSON shape would
remove three copies of the same block and fix the missing `detail` in `/visits/recent` (K15).
Rule: `AGENTS.md` defines the explicit `try/except` pattern as the convention. Do not refactor to
a handler unless asked; keep the JSON shape byte-compatible if you do.
Repo: `GAP` (optional).

**BP-APP-06 — Escape what you put into HTML.** [G]
Rule: anything not fully server-controlled goes through `html.escape` before an f-string.
Repo: `GAP` (K16; safe today only because values come from env/system).

## 4. PostgreSQL layer

**BP-DB-01 — Size pools with arithmetic, not hope.** [V S12]
Rule: keep `nodes x POSTGRES_POOL_MAX` clearly below PostgreSQL `max_connections` (default 100),
leaving headroom for `psql` and admin sessions. If instances x pool exceeds the limit, put a
pooler (PgBouncer) in front instead of raising limits.
Repo: `OK` (documented in `config.py` and `reference.md`).

**BP-DB-02 — Two short, separate timeouts.** [V S12] [G]
Rule: one timeout bounds connect + waiting for a pooled connection; another bounds each query.
Both stay short so an outage becomes a fast 503. Warn in docs that a slow database can cause
false 503s.
Repo: `OK` (1 s each).

**BP-DB-03 — If PgBouncer in transaction mode is ever added, disable prepared statements.** [V S2]
Rule: create the pool with `statement_cache_size=0`. Symptom of forgetting it:
`prepared statement "__asyncpg_stmt_N__" does not exist`, intermittent, while every infra metric
looks healthy.
Also in transaction mode: session state (`SET`, `LISTEN/NOTIFY`, temp tables, session-level
advisory locks) breaks [V S2]. `pg_advisory_xact_lock` in `_ensure_schema` is transaction-scoped
and stays inside one transaction, so it is compatible [I].
Repo: `N/A today` (no PgBouncer; adding one is an "Ask first" infrastructure change).

**BP-DB-04 — Do the whole read-modify-write in SQL.** [G]
Rule: `INSERT ... ON CONFLICT DO UPDATE ... RETURNING`, never `SELECT` then `UPDATE` in Python.
Repo: `OK`.

**BP-DB-05 — Hot row: hold its lock for as little time as possible.** [V S7] [I]
Why: many sessions updating one row serialize on its row lock. Sources recommend (a) shorter
transactions, (b) sharding the counter over several rows and summing on read, or (c) append-only
increments aggregated on read.
Repo: the single shared counter is `INTENTIONAL` (K27). One cheap idea for `record_visit` [I]:
the counter upsert runs first, so its lock is held while the log insert and trim also run.
Moving the counter upsert to the end of the transaction shortens the hold time:

```python
async with conn.transaction():
    await conn.execute(_INSERT_RECENT_SQL, node, client)
    await conn.execute(_TRIM_RECENT_SQL, max_recent)
    total = await conn.fetchval(_INCR_COUNTER_SQL, _VISITS_COUNTER)   # hot row last
```

The statement order is consistent across transactions, so no new deadlock risk. Contention may
move to the log rows, so benchmark before and after (use a script against `/visits`) and say so.
Sharding or append-only changes the schema: "Ask first".

**BP-DB-06 — Idempotent schema bootstrap under a lock.** [G]
Rule: `CREATE ... IF NOT EXISTS` inside one transaction guarded by `pg_advisory_xact_lock` so two
starting nodes do not race. Know the limit: it never alters an existing table (K26).
Repo: `OK`.

**BP-DB-07 — Every growing table has a TTL or cap, and cleanup is idempotent.** [G]
Rule: index the expiry column; make the cleanup `DELETE` safe to run on every node at once.
Repo: `OK` (`whoami_sessions_expires_idx`, per-node purge, log capped at 20).

**BP-DB-08 — Parameters only, and validate input before it reaches the driver.** [G] [V K17]
Rule: values go through `$n`. Reject malformed external values early: a NUL byte or an oversized
value in a session cookie makes the query fail and the client sees a misleading 503 (K17).
Repo: `GAP` for cookie validation, see BP-SS-02.

**BP-DB-09 — Close the pool with a bound; terminate if closing hangs.** [G]
Repo: `OK` (`wait_for(pool.close(), 2.0)` then `terminate()`).

## 5. Health checks

**BP-HC-01 — Liveness checks the process only.** [V S6]
Rule: no database call in liveness. Otherwise a database outage makes the orchestrator restart
every healthy node, which makes things worse.
Repo: `OK` (`/health`).

**BP-HC-02 — Be careful with dependency checks in anything that removes nodes from rotation.** [V S6]
Why: when a shared dependency fails, every node fails readiness at the same moment, so the whole
service becomes unreachable. Prefer degraded answers (what this app does with 503 per endpoint).
Rule: load balancers and compose healthchecks use `/health`. `/health/ready` is for humans and
monitoring.
Repo: `OK` (forbidden in `AGENTS.md`).

**BP-HC-03 — Keep readiness cheap and bounded.** [V S6]
Rule: `SELECT 1` through the normal timeouts; if something polls often, cache the result for a few
seconds rather than hitting the database per probe.
Repo: `OK` for a lab; no cache.

**BP-HC-04 — Container healthcheck targets `/health`, not `/docs`.** [V S9]
Repo: `OK` in compose; `GAP` in the Dockerfile (K24).

**BP-HC-05 — Balancer health is passive.** [G]
Apache `mod_proxy_balancer` learns that a member is dead when a real request to it fails, marks
it as in error, and tries it again after the `retry` interval from `BalancerMember`. Expect a few
slow first requests after a node dies.
Repo: `OK` (the recipe uses `retry=10`; no active probing exists or is needed).

## 6. Reverse proxy (Apache) and uvicorn

**BP-PX-01 — Trust forwarded headers only from the proxy.** [V S3]
Rule: `--proxy-headers --forwarded-allow-ips=<proxy ip or CIDR>`. uvicorn trusts only
`127.0.0.1` by default and accepts IPs, networks and UNIX socket paths.
Never use `*` here: ports 8001/8002 are published, so a direct client could forge
`X-Forwarded-For`. Many copy-paste examples use `*`; they are wrong for this repo.
Repo: `GAP` (K9). Changing the `CMD` or compose network is "Ask first".

**BP-PX-02 — Make the proxy the side that closes idle upstream connections.** [V S5] [G]
Why: uvicorn closes idle keep-alive connections after 5 s (`--timeout-keep-alive`, default). A
reverse proxy that reuses upstream connections for longer can send a request on a connection that
is being torn down and returns a sporadic `502`. The race cannot be removed completely, only made
unlikely by ordering the timeouts: backend idle timeout > proxy idle timeout.
Fix (choose one):

```dockerfile
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--timeout-keep-alive", "65"]
```

or make Apache retire idle backend connections sooner than 5 s: `ttl=4` on each `BalancerMember`
(or `disablereuse=On`, at the cost of a new TCP connection per request) [G].
Symptom to look for: Apache error log lines like `error reading status line from remote
server` (`End of file found`), mostly on the first request after a quiet period. Reproduce by
waiting 6+ seconds between requests.
Repo: `GAP`, not yet in `known-issues.md` (suggest K29). "Ask first" (changes `CMD`).

**BP-PX-03 — Do not make the balancer treat 503 from the app as a dead node.** [I]
Why: a storage outage makes every node return 503 for data endpoints. If the balancer counts
5xx answers as member failures (Apache `BalancerMember ... failonstatus=503` is one way to ask
for that [G]), it marks all nodes as in error, and then even `/` and `/health` return 502 or 503.
That breaks the core promise (P3). Plain connection failures are the right failure signal.
Repo: `OK` (the recipe does not set it).

**BP-PX-04 — A retried GET is a repeated side effect.** [I] [G]
A proxy may retry a GET on another node after a connection error or timeout. `/visits` is a GET
that writes (K12), so a retry can double-count. Accept it in the lab. Never probe `/visits`.

**BP-PX-05 — Order timeouts.** [V S5] [G]
Rule: the proxy's backend timeout (Apache `ProxyTimeout`) must exceed the slowest legitimate
handler (`/delay` max 10 s); database timeouts must be much shorter than proxy timeouts so the
app answers 503 before the proxy gives up.
Repo: `OK` (1 s database timeouts, 10 s maximum `/delay`).

**BP-PX-06 — Optional backpressure.** [V S3]
`--limit-concurrency N` makes uvicorn answer 503 above N concurrent requests. The body would not
match the project 503 shape; ask first.

## 7. Sessions and cookies

**BP-SS-01 — Opaque random server-issued ID, state on the server.** [V S8]
OWASP: IDs must come from a CSPRNG, carry at least 64 bits of entropy (128-bit length is the
common target), and mean nothing by themselves; session data lives in server-side storage.
Repo: `OK` (`uuid4().hex`, about 122 random bits; data in PostgreSQL).

**BP-SS-02 — Validate the shape of a client-supplied session ID.** [V S8] [G]
Rule: accept only the format you issue; otherwise issue a new ID. This also fixes the NUL-byte and
oversized-value failures (K17).

```python
_SESSION_ID_RE = re.compile(r"^[0-9a-f]{32}$")   # module level, needs `import re`
raw = request.cookies.get(settings.session_cookie_name)
session_id = raw if raw and _SESSION_ID_RE.fullmatch(raw) else uuid.uuid4().hex
```

Limit: a well-formed but never-issued ID is still accepted and gets a row (fixation-style). For a
login-less lab that is acceptable; do not add auth.
Repo: `GAP` (K17).

**BP-SS-03 — Cookie attributes.** [V S8]
Always `HttpOnly`; `SameSite=Lax` (or `Strict`); `Secure` whenever every client uses HTTPS.
TLS terminates on the proxy, so the app cannot see HTTPS: that is why `SESSION_COOKIE_SECURE` is
an env flag. The `__Host-` name prefix is the strongest option (requires `Secure`, `Path=/`, no
`Domain`) but browsers reject it over plain HTTP, and renaming the cookie is a contract change.
Repo: `OK` (flag, default off for the HTTP lab). The prefix is "Ask first".

**BP-SS-04 — Expiry is enforced on the server.** [V S8]
The cookie `max_age` is a hint; `expires_at` in PostgreSQL decides. A row past `expires_at` counts
as absent even if the purge task has not deleted it.
Repo: `OK`.

## 8. Configuration and containers

**BP-CFG-01 — Configuration from environment only; one image for every node.** [G]
Repo: `OK`. Node differences go under `environment:` in compose, never into the image.

**BP-CFG-02 — Check invariants early.** [I]
`Settings` reads env at import time, so a bad number fails at import (good) but cross-field
mistakes (nodes x pool > `max_connections`) are not caught. Document them (done); a validator is
a dependency or code change, "Ask first".

**BP-CFG-03 — Defaults are for the lab, secrets are not committed.** [G]
Repo: `OK` with caveats K21.

**BP-DK-01 — Exec-form `CMD`.** [V S9] So SIGTERM reaches uvicorn and shutdown (lifespan, pool close)
runs. Repo: `OK`.

**BP-DK-02 — Run as non-root.** [V S9] Repo: `OK`.

**BP-DK-03 — Add `.dockerignore`.** [V S9] Keeps `.git`, `.venv`, `.env`, caches out of the build
context. Repo: `GAP` (K22).

**BP-DK-04 — `HEALTHCHECK` in the Dockerfile against `/health`.** [V S9] Use `python -c urllib...`;
the slim image has no `curl`. Repo: `GAP` (K24).

**BP-DK-05 — Reproducible builds.** [G] Pin direct dependencies (done), add a lock file or hashes
for transitive ones (K25), keep the local Python equal to the image's. The upload that produced
this research contained `*.cpython-313.pyc` files while the image is `python:3.12-slim`: align
versions and never commit `__pycache__` (a `Never` rule).

**BP-DK-06 — Single-stage build is fine here.** [G] Multi-stage helps when you compile native
extensions; `asyncpg` and `uvicorn[standard]` ship prebuilt wheels for CPython 3.12.

**BP-DK-07 — Graceful shutdown has a deadline.** [V S5] [G] Compose waits 10 s by default
before SIGKILL. `--timeout-graceful-shutdown` bounds uvicorn's own wait. Keep it above the longest
handler (`/delay` 10 s) only if you care about draining.

## 9. Testing

**BP-T-01 — SQL needs the real engine.** The repo's `testing.md` already explains why. Do not mock
asyncpg to "test" `ON CONFLICT` or `make_interval`.

**BP-T-02 — Lifespan must run in tests.** [V S10] `with TestClient(app)` runs it. An
`httpx.AsyncClient(transport=ASGITransport(app))` does not; wrap it in `LifespanManager` from
`asgi-lifespan`. Without lifespan, the pool is never closed and the purge task never starts.

**BP-T-03 — Cover the failure path as carefully as the happy path.** Patch `db_manager._get_pool`
to raise `OSError` and assert: `/health`, `/api` return 200; `/health/ready`, `/visits`, `/session`
return 503 with `error == "storage_unavailable"`.

**BP-T-04 — Skipped is not passed.** Always run `pytest -rs` and report skips.

**BP-T-05 — Assert the contract.** `X-Node-*` headers on every response, `served_by_node` on
endpoints that work, the 503 body shape.

**BP-T-06 — The pool is bound to one event loop.** Use one `with TestClient(app)` per test.

## 10. Where sources and this repo disagree (resolution)

| Topic | Common advice | This repo | Resolution |
|-------|---------------|-----------|------------|
| Workers | gunicorn + uvicorn workers [S3] | one process per container | follow repo (BP-APP-04) |
| Proxy trust | `--forwarded-allow-ips="*"` in tutorials | specific proxy only | follow repo (BP-PX-01) |
| Startup | fail if the DB is missing [S1] | lazy pool, app starts without DB | follow repo (BP-APP-03) |
| Readiness | check dependencies [S6] | exists, but never used for balancing | follow repo (BP-HC-02) |
| Health target | `/docs` in generators [S9] | `/health` | follow repo |
| Versions | newer uvicorn/FastAPI in 2026 skill catalogs | pinned 0.30.6 / 0.115.0 | do not upgrade unasked (K25 area) |
| Cookie name | `__Host-` prefix [S8] | `whoami_session` | HTTP lab; "Ask first" |

## 11. Gap list (candidates; each is a request, not a TODO)

| Priority | Gap | Smallest change | Touches "Ask first" | Ref |
|----------|-----|-----------------|---------------------|-----|
| 1 | idle keep-alive race gives sporadic 502 | BP-PX-02 | yes (`CMD`) | new K29 |
| 2 | forwarded headers not trusted from the proxy | BP-PX-01 | yes (`CMD`, compose network) | K9 |
| 3 | session cookie value not validated | BP-SS-02 | no (internal behavior) | K17 |
| 4 | no `.dockerignore`, no Dockerfile `HEALTHCHECK`, loose transitive pins | BP-DK-03..05 | no | K22 K24 K25 |
| 5 | blocking `getaddrinfo` per request | compute once at startup | no | K11 |
| 6 | `/visits/recent` 503 lacks `detail` | add field, or BP-APP-05 | shape change: yes | K15 |
| 7 | counter lock held during log insert and trim | BP-DB-05 reorder, measure | no | K27 |
| 8 | HTML values unescaped | `html.escape` | no | K16 |

## 12. Checklist before you finish a change

1. Did you keep shared state only in PostgreSQL, accessed only via `DatabaseManager`? (P1, P4)
2. Does every new wait have a timeout shorter than the proxy's? (P2, P8)
3. Does a `StorageUnavailable` produce the 503 JSON shape, never a 500? (P3)
4. Does the new data endpoint return `served_by_node`? Is the new SQL parameterized and atomic? (P5)
5. Is anything from the request (cookie, header, query) validated or escaped before use? (P6)
6. Did you avoid `/visits`, `/session`, `/health/ready` as probe targets? (P7)
7. Did you update README, `reference.md` and the `K` entry you touched? (P9)
8. Did you keep CRLF line endings and say exactly what you could not run (Docker, PostgreSQL, Apache)?

## 13. Writing docs that LLM agents use well

Findings from an analysis of 2,500+ agent instruction files [S11], and how this repo's docs match:
- Put executable commands first, with exact flags (done: `AGENTS.md` Commands).
- Use three tiers: Always / Ask first / Never (done).
- Be specific and concrete. "Be helpful" instructions do not help; examples and numbers do.
- Keep the root file short; link deeper docs and say when to read them (progressive disclosure; done).
- Keep `CLAUDE.md` and `AGENTS.md` consistent; the `@AGENTS.md` import achieves that.
- Documentation must describe real behavior, not intentions. Mark verified vs unverified (done in
  `testing.md`, `known-issues.md`; this file uses `[V]/[I]/[G]`).

## 14. Sources

Retrieved on 2026-10-09. S4 was withdrawn; the ID is not reused. Quality: P = primary/official, S = secondary (blog, vendor guide, catalog).

| ID | Topic | Source | Q |
|----|-------|--------|---|
| S1 | FastAPI lifespan, async traps, per-worker lifespan | https://gravitee.io/corpus/gen-2205/fastapi/fastapi-api-lifecycle-management.html ; https://skills.sh/ingpdw/pdw-python-dev-tool/app-scaffolding ; https://tomevault.io/tome/modbender/skill-library-mcp/fastapi | S |
| S2 | asyncpg + PgBouncer, `statement_cache_size=0` | https://github.com/MagicStack/asyncpg/pull/1065 ; https://www.netdata.cloud/guides/pgbouncer/pgbouncer-prepared-statement-does-not-exist/ | P / S |
| S3 | uvicorn proxy headers, `--forwarded-allow-ips`, `--limit-concurrency`, deployment | https://www.uvicorn.org/settings ; https://www.uvicorn.org/deployment/ | P |
| S5 | keepalive timeout ordering and 502 race | https://www.youngju.dev/blog/network/2026-07-26-http-keepalive-and-connection-reuse.en ; https://markaicode.com/errors/uvicorn-timeout-fix/ | S |
| S6 | liveness vs readiness, shared-dependency risk | https://aws-samples.github.io/sample-apex-skills/docs/skills/eks-best-practices/references/reliability-core ; https://oneuptime.com/blog/post/2026-01-24-kubernetes-liveness-readiness-probes/view ; https://www.develeap.com/?p=1780 | S |
| S7 | PostgreSQL hot-row contention, sharded counters | https://www.netdata.cloud/guides/postgres/postgres-row-level-lock-contention/ ; https://oneuptime.com/blog/post/2026-01-24-fix-lock-contention-issues/view ; https://postgres.ai/chats/0191a496-aef1-7818-95db-6696b28e6f3a | S |
| S8 | session IDs and cookies | https://cheatsheetseries.owasp.org/cheatsheets/Session_Management_Cheat_Sheet.html ; https://specification.website/spec/security/cookie-attributes/ | P / S |
| S9 | Docker for FastAPI | https://betterstack.com/community/guides/scaling-python/fastapi-docker-best-practices/ ; https://easypanel.io/dockerizer/fastapi | S |
| S10 | async tests and lifespan | https://fastapi.tiangolo.com/advanced/async-tests/ ; https://til.simonwillison.net/asgi/lifespan-test-httpx | P / S |
| S11 | writing agent instruction files | https://github.blog/ai-and-ml/github-copilot/how-to-write-a-great-agents-md-lessons-from-over-2500-repositories/ ; https://docs.atlan.com/agents/how-tos/write-an-agents-md-file | S |
| S12 | pool sizing and timeouts | https://blog.easecloud.io/cloud-infrastructure/optimizing-database-connections-and-connection-pooling/ | S |

Not researched (so no rules here): structured logging and tracing, metrics, rate limiting, CI,
PostgreSQL replication/failover. Add them only if the lab scope grows.
