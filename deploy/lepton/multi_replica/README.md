# Several Omnigent replicas on one box (host sharding)

Fork-owned. This runs N Omnigent server replicas behind one NGINX router on a
single machine. It uses upstream's host sharding: every client that talks
about a host sends that host's id as a slice key, and NGINX hashes the key so
the host, its runners and the browser requests for that host all reach the
same replica. Upstream's Kubernetes version of this is
`deploy/kubernetes/multi_replica/`; read its README for the design.

| File | Purpose |
|---|---|
| `docker-compose.yaml` | `replica-0` … `replica-N`, plus `router` (NGINX) |
| `nginx.conf` | slice-key map, websocket upgrade, long timeouts, `X-Omnigent-Upstream` |
| `upstreams.conf` | one `server` line per replica, `hash $omnigent_host_key consistent` |
| `env.example` | copy to `.env`: image tag, external `DATABASE_URL`, auth |
| `verify.py` | rehearsal check with a real host and runner (auth-off stacks only) |

## What is shared and what is not

- **Shared:** Postgres (external, `DATABASE_URL`) and one `/data` volume
  (artifacts, the accounts cookie secret, the admin roster). Every replica
  reads the same `.env`.
- **Per replica:** live host and runner tunnels, terminal sockets, the SSE
  streams it serves, and in-memory caches (the harness cache's 30 s TTL is
  the staleness bound for agent edits made on another replica).
- **Scheduler:** only `replica-0` arms recurring scheduled tasks. Every other
  replica runs with `OMNIGENT_DISABLE_SCHEDULER=1`. Upstream has no
  scheduler leasing yet, so two schedulers would fire every task twice.

Requests that carry no slice key (session list, search, settings, agents)
have no host affinity. NGINX's hash balancer round-robins an empty key, and
any replica answers them from Postgres. Live sidebar fields (runner and host
online) are mirrored to Postgres, so they are correct on every replica.

## Prerequisites (all of them, before the first second replica starts)

1. **Server image built with host routing in the web bundle:**

   ```bash
   docker build -f deploy/docker/Dockerfile \
     --build-arg VITE_OMNIGENT_HOST_ROUTING=true \
     -t <registry>/omnigent-server:<commit> .
   ```

   Without the build arg the browser never sends the slice key. File browsing,
   terminals and other host-scoped calls then land on a random replica and
   fail whenever it is not the host's.

2. **Every host upgraded, with `OMNIGENT_HOST_SLICE_KEY_ENABLED=1`.** The
   host passes it to its runners (`OMNIGENT_RUNNER_SLICE_KEY`), and both then
   stamp `X-Databricks-Omnigent-Slice-Key: <host_id>` on their tunnels and
   HTTP calls. A host that is on an old CLI, or lacks the variable, sends no
   key: its tunnel and its runners' tunnels round-robin across replicas,
   so file reads, terminals and turn dispatch break for that host. There is
   no server-side fallback. Check every host before scaling past one replica:

   ```bash
   # on each host machine
   omnigent --version
   systemctl --user show omnigent-host -p Environment | grep SLICE_KEY
   ```

   During the upgrade wave keep ONE replica: the variable is harmless with a
   single replica (the key is only a routing hint).

3. **Server-side `OMNIGENT_HOST_SHARDED=1`** (set by `docker-compose.yaml`).
   The Docker entrypoint passes it to `create_app(host_sharded=True)`, so a
   request for a live host that reached the wrong replica answers
   `400 wrong_replica` and the web client re-addresses, instead of a
   misleading `409 host is offline`.

4. **A fixed `OMNIGENT_ACCOUNTS_COOKIE_SECRET`** (accounts auth) in `.env`, so
   replicas never race to generate one on the shared volume.

## Rollout

1. Land the image (sync + this wiring) and run it as a single replica first
   (today's deployment, plus `VITE_OMNIGENT_HOST_ROUTING=true`). Nothing
   changes for clients.
2. Upgrade every host CLI and set `OMNIGENT_HOST_SLICE_KEY_ENABLED=1`
   (prerequisite 2), host by host. Once the router is in front, its access
   log shows `key=""` for any tunnel from a host that was missed.
3. Rehearse on a scratch box with `verify.py` (below).
4. Put this compose project in front, with `replica-1` added. Move the public
   port from the single server to `router`. The replicas run migrations at
   boot; compose starts `replica-1` only after `replica-0` is healthy, so
   only one replica migrates.
5. Watch `X-Omnigent-Upstream` / the router access log: each host's
   `key="<host_id>"` lines should name one upstream.

Adding a replica changes which replica about 1/N of hosts hash to. Their
tunnels reconnect (hosts back off 0.5 to 3 s, runners 0.5 to 10 s) and land
on the new owner. Expect a brief blip for those hosts, not an outage.

To add a replica: copy a `replica-N` block in `docker-compose.yaml` (keep
`OMNIGENT_DISABLE_SCHEDULER: "1"`), add `server replica-N:8000 resolve
max_fails=0;` to `upstreams.conf`, then `docker compose up -d replica-N` and
`docker compose exec router nginx -s reload`.

## Rollback

- **To one replica, same image:** `docker compose stop replica-1` (and any
  others), remove their `server` lines, `nginx -s reload`. Hosts on the
  stopped replicas reconnect to `replica-0`. `replica-0` keeps the
  scheduler, so nothing double-fires or goes missing.
- **To the previous single-server deployment:** point the public port back at
  the old service and `docker compose down` this project. Hosts keep sending
  the slice key; a single server ignores it.
- **Image rollback:** set the previous `OMNIGENT_IMAGE_TAG` in `.env` and
  `docker compose up -d`. This wiring adds no migration.

## Rehearsal (local, auth off, scratch Postgres)

```bash
cd deploy/lepton/multi_replica
cat > /tmp/omni-mr.env <<EOF
OMNIGENT_IMAGE=omnigent-server
OMNIGENT_IMAGE_TAG=local
DATABASE_URL=postgresql+psycopg://omnigent:omnigent@<pg-host>:5432/omnigent
OMNIGENT_AUTH_ENABLED=0
OMNIGENT_LOCAL_SINGLE_USER=1
EOF
OMNIGENT_ENV_FILE=/tmp/omni-mr.env OMNIGENT_IMAGE=omnigent-server OMNIGENT_IMAGE_TAG=local \
  OMNIGENT_ROUTER_BIND=127.0.0.1 OMNIGENT_ROUTER_PORT=18000 \
  COMPOSE_PROJECT_NAME=omni-mr OMNIGENT_DATA_VOLUME=omni-mr-data \
  docker compose up -d
TMPDIR=/tmp uv run python deploy/lepton/multi_replica/verify.py \
  --url http://127.0.0.1:18000 --router-container omni-mr-router-1 --replicas 2
```

`verify.py` runs a real host from the checkout with an isolated `HOME`,
launches a runner and a terminal, and prints `PASS` when every check holds.
It refuses non-loopback URLs. Keep `TMPDIR` short (e.g. `/tmp`): the runner
opens Unix sockets under it and a deep path fails with `AF_UNIX path too long`.

## Limits

- One box: a box failure takes every replica down. This spreads load across
  processes; it is not high availability.
- One router and one Postgres.
- Agent edits made on one replica can show a stale harness on another for up
  to `OMNIGENT_HARNESS_CACHE_TTL_S` (30 s).
