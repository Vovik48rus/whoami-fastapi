# Recipes

Proxy snippets below are templates; they were not run in this repository. Validate
with `nginx -t` or `apachectl configtest`.

## Add an endpoint

1. Add an `async def` handler in the matching section of `app/main.py` (the file is
   split by banner comments: identity, middleware, whoami, health, shared state,
   sessions, helpers).
2. If it does work or touches data, return `served_by_node`. If it uses Redis, follow
   the 503 pattern from `AGENTS.md` and add any new keys to `reference.md`.
3. Add a row to the endpoint table in `README.md` and `reference.md`.
4. Run the smoke test.

## Add an environment variable

1. Add a field to `Settings` in `app/config.py` with type conversion and a Russian comment.
2. Add it to `.env.example`, the README, and the table in `reference.md`.
3. If it differs per node, set it under `environment:` of that service in
   `docker-compose.yml`. Never bake node differences into the image.

## Add a third node

1. In `docker-compose.yml` copy the `app2` block to `app3`: new `container_name`,
   `NODE_NAME: node-3`, a unique host port such as `"8003:8000"`.
2. Add `server app3:8000 max_fails=3 fail_timeout=10s;` to the `upstream` in `nginx.conf`.
3. `docker compose up -d --build`, then `docker compose exec nginx nginx -s reload`.
4. Confirm three nodes rotate in `X-Node-Name`.

`docker compose up --scale app1=N` does not work here: fixed `container_name` and host
`ports` conflict, so use explicit services.

## TLS termination on Nginx

```nginx
server {
    listen 80;
    server_name _;
    return 301 https://$host$request_uri;
}

server {
    listen 443 ssl;
    server_name example.local;

    ssl_certificate     /etc/nginx/certs/server.crt;
    ssl_certificate_key /etc/nginx/certs/server.key;
    ssl_protocols       TLSv1.2 TLSv1.3;

    location / {
        proxy_pass http://whoami_backend;
        proxy_http_version 1.1;
        proxy_set_header Connection "";
        proxy_set_header Host              $host;
        proxy_set_header X-Real-IP         $remote_addr;
        proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

Verify with `curl -k -s https://example.local/api`: the echoed headers should contain
`x-forwarded-proto: https` while the app stays unchanged. Self-signed cert for a lab:
`openssl req -x509 -newkey rsa:2048 -nodes -days 30 -keyout server.key -out server.crt -subj "/CN=example.local"`.
Never commit keys or certificates. The app reads `X-Forwarded-Proto` at most; it must
not terminate TLS itself.

## L4 balancing with Nginx `stream`

`stream {}` belongs at the top level of a full `/etc/nginx/nginx.conf`, next to
`http {}`. It is invalid inside `conf.d/default.conf`, which is included in `http {}`.
The stream module is built into the official images; Debian/Ubuntu need
`libnginx-mod-stream`.

```nginx
stream {
    upstream whoami_l4 {
        server 127.0.0.1:8001;
        server 127.0.0.1:8002;
    }
    server {
        listen 9000;
        proxy_pass whoami_l4;
    }
}
```

At L4 the proxy cannot add `X-Forwarded-*` or `X-Real-IP`, so they will be absent from
the echo and `remote_addr` is the proxy. That difference is the point of the demo.
TLS can be terminated at L4 with `listen ... ssl` in `stream` (`ngx_stream_ssl_module`).
TLS passthrough would require TLS in the app and is not allowed.

## Apache as the reverse proxy

Needs modules `proxy`, `proxy_http`, `proxy_balancer`, `lbmethod_byrequests`, `headers`
(and `ssl` for HTTPS):

```apache
<Proxy "balancer://whoami">
    BalancerMember "http://127.0.0.1:8001" retry=10
    BalancerMember "http://127.0.0.1:8002" retry=10
    ProxySet lbmethod=byrequests
</Proxy>

ProxyPreserveHost On
RequestHeader set X-Forwarded-Proto "http"      # "https" in the HTTPS vhost
ProxyPass        "/" "balancer://whoami/"
ProxyPassReverse "/" "balancer://whoami/"
```

`mod_proxy_http` adds `X-Forwarded-For` itself. Show L4 on Nginx `stream`; Apache's L4
options are less convenient.

## DNS round-robin

Publish several `A` records for one name, pointing at different proxy or node
addresses. DNS has no health awareness, resolvers cache answers, and record order
rotates. Check with repeated `dig +short example.local` and `getent ahosts example.local`.
`/etc/hosts` does not rotate; use dnsmasq or BIND for a lab. Switching nodes between
requests is safe because state lives in Redis.

## Other Nginx algorithms

`least_conn;` balances by active connections. `ip_hash;` gives stickiness and is not
needed here: sessions live in Redis, and the demo shows that stickiness is unnecessary.
Do not enable sticky "for safety"; it hides state-storage bugs.
