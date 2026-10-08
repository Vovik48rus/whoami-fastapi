# Operations

Day-to-day commands are in `AGENTS.md`. This file covers failure scenarios,
inspection and Nginx specifics. Examples are bash (use Git Bash/WSL on Windows).

## Compose stack

`docker compose up --build -d` starts `redis`, `app1` (host `:8001`) and `app2`
(host `:8002`). Nodes are published directly, bypassing any balancer. There is no
nginx container; see below to add one.

```bash
curl -s localhost:8001/            # node-1 whoami dump
curl -s localhost:8002/api         # node-2 JSON
docker compose stop app1           # kill a node
docker compose start app1          # bring it back
docker compose down -v             # stop and delete the redis-data volume
```

## Smoke test and expected results

| Command | Expect |
|---------|--------|
| `curl -s localhost:8001/health` | 200, `{"status":"ok",...}` |
| `curl -s -o /dev/null -D - localhost:8001/bench \| grep -i '^x-node'` | three `X-Node-*` headers |
| `/visits` on `:8001`, then `:8002` | counter grows monotonically across both nodes |
| `curl -s -c jar localhost:8001/session`, then `curl -s -b jar -c jar localhost:8002/session` | same `session_id`, growing `hits_in_session`, different `served_by_node` |
| `curl -s 'localhost:8001/delay?ms=300'` | `actual_delay_ms` close to 300 |
| `curl -s -o /dev/null -w '%{http_code}\n' -I localhost:8001/health` | `405` (known: no HEAD support) |

## Failure scenarios

Redis down:

```bash
docker compose stop redis
for p in health api health/ready visits session; do
  printf '%s -> ' "$p"; curl -s -o /dev/null -w '%{http_code}\n' localhost:8001/$p
done
# expect 200, 200, 503, 503, 503
docker compose start redis   # endpoints should recover without restarting nodes (expected, not verified here)
```

Node down: `docker compose stop app1`; requests to `:8002` keep working and the shared
counter and sessions are intact. Through a balancer, expect extra latency (up to
`proxy_connect_timeout`, 3 s) on the first requests until Nginx marks the node failed.

## Redis inspection

```bash
docker compose exec redis redis-cli --scan --pattern 'whoami:*'   # not KEYS *
docker compose exec redis redis-cli GET whoami:global:visits
docker compose exec redis redis-cli LRANGE whoami:global:recent_requests 0 -1
docker compose exec redis redis-cli TTL whoami:session:<session_id>
docker compose exec redis redis-cli DEL whoami:global:visits      # reset the counter
```

## Nginx

`nginx.conf` is a `conf.d`-level fragment (`upstream` + `server`, no `events`/`http`).
Mount it as `/etc/nginx/conf.d/default.conf`, or place it in `conf.d/` or
`sites-available/` on a host. It cannot be passed to `nginx -c`.

Option A — add the service back to `docker-compose.yml`:

```yaml
  nginx:
    image: nginx:1.27-alpine
    container_name: whoami-nginx
    restart: unless-stopped
    ports:
      - "8080:80"
    volumes:
      - ./nginx.conf:/etc/nginx/conf.d/default.conf:ro
    depends_on:
      - app1
      - app2
    networks:
      - backend-net
```

Option B — run a one-off container on the compose network:

```bash
docker network ls | grep backend-net     # name is <project-dir>_backend-net
docker run --rm -p 8080:80 --network <project-dir>_backend-net \
  -v "$PWD/nginx.conf:/etc/nginx/conf.d/default.conf:ro" nginx:1.27-alpine
```

Check balancing:

```bash
for i in $(seq 1 6); do
  curl -s -o /dev/null -D - http://localhost:8080/bench | grep -i '^x-node-name'
done                                   # expect node-1 / node-2 alternating
```

`docker compose exec nginx nginx -t` validates the config. Running `nginx -t` in a
container outside the compose network fails with `host not found in upstream`; that is
not a config error.

Docker-specific pitfalls:

- Upstream names resolve once at start or reload. If `app1` restarts with a new IP,
  Nginx keeps the old one until `docker compose exec nginx nginx -s reload`.
- Nginx will not start while an upstream host does not resolve (for example `app1`
  stopped during a manual Nginx restart).
- Only passive checks exist (`max_fails=3 fail_timeout=10s`); open-source Nginx has no
  active health checks.
- `keepalive 32`, `proxy_http_version 1.1` and `proxy_set_header Connection ""` work
  together for connection reuse. Do not remove one of them alone.
