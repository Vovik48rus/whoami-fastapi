# Recipes

Proxy snippets below are templates; they were not run in this repository. Validate
with `apachectl configtest`.

## Add an endpoint

1. Add an `async def` handler in the matching section of `app/main.py` (the file is
   split by banner comments: identity, middleware, whoami, health, shared state,
   sessions, helpers).
2. If it does work or touches data, return `served_by_node`. If it uses the database,
   add a method to `DatabaseManager` (rules in `reference.md`), follow the 503 pattern
   from `AGENTS.md` and document any new table or column in `reference.md`.
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
2. If Apache balances the nodes, add `BalancerMember "http://127.0.0.1:8003" retry=10` to the
   `balancer://whoami` block (see "Apache as the reverse proxy") and reload Apache.
3. `docker compose up -d --build`.
4. Confirm the third node answers on `:8003` (`X-Node-Name: node-3`), and that three nodes
   rotate in `X-Node-Name` when requests go through Apache.

`docker compose up --scale app1=N` does not work here: fixed `container_name` and host
`ports` conflict, so use explicit services.

## TLS termination on Apache

Needs `ssl`, `headers`, `proxy`, `proxy_http` and the balancer from the section below.

```apache
<VirtualHost *:80>
    ServerName example.local
    Redirect permanent / https://example.local/
</VirtualHost>

<VirtualHost *:443>
    ServerName example.local

    SSLEngine on
    SSLCertificateFile    /etc/apache2/certs/server.crt
    SSLCertificateKeyFile /etc/apache2/certs/server.key
    SSLProtocol           -all +TLSv1.2 +TLSv1.3

    # <Proxy "balancer://whoami"> from the next section goes here or at server level.
    ProxyPreserveHost On
    RequestHeader set X-Forwarded-Proto "https"
    ProxyPass        "/" "balancer://whoami/"
    ProxyPassReverse "/" "balancer://whoami/"
</VirtualHost>
```

Verify with `curl -k -s https://example.local/api`: the echoed headers should contain
`x-forwarded-proto: https` while the app stays unchanged. Self-signed cert for a lab:
`openssl req -x509 -newkey rsa:2048 -nodes -days 30 -keyout server.key -out server.crt -subj "/CN=example.local"`.
Never commit keys or certificates. The app reads `X-Forwarded-Proto` at most; it must
not terminate TLS itself.

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

`mod_proxy_http` adds `X-Forwarded-For` itself. `mod_proxy_balancer` works at L7 (HTTP);
there is no L4 recipe in this repo.

## DNS round-robin

Publish several `A` records for one name, pointing at different proxy or node
addresses. DNS has no health awareness, resolvers cache answers, and record order
rotates. Check with repeated `dig +short example.local` and `getent ahosts example.local`.
`/etc/hosts` does not rotate; use dnsmasq or BIND for a lab. Switching nodes between
requests is safe because state lives in PostgreSQL.

## Sticky sessions

Do not enable them (Apache `stickysession=`, `ProxySet stickysession=...`). Sessions live in
PostgreSQL, and the demo shows that stickiness is unnecessary. Sticky "for safety" hides
state-storage bugs.
