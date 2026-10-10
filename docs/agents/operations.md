# Operations

Day-to-day commands are in `AGENTS.md`. This file covers failure scenarios,
and inspection. Examples are bash (use Git Bash/WSL on Windows).

## Compose stack

`docker compose up --build -d` starts `postgres`, `app1` (host `:8001`) and `app2`
(host `:8002`). Nodes are published directly, bypassing any balancer. There is no
proxy container; to put Apache in front see `recipes.md`.

```bash
curl -s localhost:8001/            # node-1 whoami dump
curl -s localhost:8002/api         # node-2 JSON
docker compose stop app1           # kill a node
docker compose start app1          # bring it back
docker compose down -v             # stop and delete the postgres-data volume
```

## Smoke test and expected results

| Command | Expect |
|---------|--------|
| `curl -s localhost:8001/health` | 200, `{"status":"ok",...}` |
| `curl -s localhost:8001/health/ready` | 200, `{"status":"ok",...,"postgres":true}` |
| `curl -s -o /dev/null -D - localhost:8001/bench \| grep -i '^x-node'` | three `X-Node-*` headers |
| `/visits` on `:8001`, then `:8002` | counter grows monotonically across both nodes |
| `curl -s -c jar localhost:8001/session`, then `curl -s -b jar -c jar localhost:8002/session` | same `session_id`, growing `hits_in_session`, different `served_by_node` |
| `curl -s 'localhost:8001/delay?ms=300'` | `actual_delay_ms` close to 300 |
| `curl -s -o /dev/null -w '%{http_code}\n' -I localhost:8001/health` | `405` (known: no HEAD support) |

## Failure scenarios

PostgreSQL down:

```bash
docker compose stop postgres
for p in health api health/ready visits session; do
  printf '%s -> ' "$p"; curl -s -o /dev/null -w '%{http_code}\n' localhost:8001/$p
done
# expect 200, 200, 503, 503, 503
docker compose start postgres   # endpoints recover without restarting nodes
```

Recovery without restarting nodes was verified against a local PostgreSQL 16 (outage
while the app was running, and a start while the database was down). It was not run
in compose.

Node down: `docker compose stop app1`; requests to `:8002` keep working and the shared
counter and sessions are intact. Through an external balancer (Apache), expect extra latency on the first requests until it
marks the node as failed.

## PostgreSQL inspection

```bash
docker compose exec postgres psql -U whoami -d whoami -c '\dt'
docker compose exec postgres psql -U whoami -d whoami -c "SELECT * FROM whoami_counters"
docker compose exec postgres psql -U whoami -d whoami -c "SELECT * FROM whoami_recent_requests ORDER BY id DESC"
docker compose exec postgres psql -U whoami -d whoami -c "SELECT session_id, hits, expires_at - now() AS ttl FROM whoami_sessions"
docker compose exec postgres psql -U whoami -d whoami -c "UPDATE whoami_counters SET value = 0 WHERE name = 'global:visits'"   # reset the counter
```

Tables appear after the first data request that reaches a healthy database. `\dt` on a
fresh stack that has never been hit shows nothing; call `/visits` once.
