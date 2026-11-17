# Multi-replica host sharding: static adversarial review (fork main @ 92413b705, 2026-10-22)
Static review only; nothing was run. "Confirmed" = traced through the code; "suspected" = not reproduced.

## Blockers
1. **The web client never retries `wrong_replica` in the OSS build (confirmed).**
   - Every retry branch is gated on `isDatabricksWorkspace()` (`web/src/lib/identity.ts:632,651,674`).
   - `VITE_OMNIGENT_HOST_ROUTING=true` only adds the key (`web/src/lib/host.ts:233-239`).
   - Fix: an OSS retry that re-resolves the host and retries with the key, with backoff.
2. **Scheduled tasks fire only for hosts on replica-0 (confirmed).**
   - The fire path checks only the local registry (`server/scheduled/fire.py:650,677,1121,1156`). About half fail with `host_offline`.
   - Fix: dispatch with the host key through the router.
3. **Scheduled-task create/update/delete on a non-scheduler replica never reaches replica-0's timers (confirmed).**
   - `routes/scheduled_tasks.py:410,662,682`; tasks load only at start (`scheduled/scheduler.py:137-148`).
   - Fix: pin `/v1/scheduled-tasks*` to replica-0, or resync from the DB.
4. **CLI ticket and mobile native login state is in process memory (confirmed).**
   - `_cli_tickets` and `_native_codes` are dicts (`routes/auth.py:250-252`). The callback is keyless, so it round-robins, and the CLI poll gets 410 (`auth.py:596,990,1093`).
   - Fix: store them in Postgres, or route `/auth/*` to one replica.
5. **Non-web clients send no key, and nothing handles `wrong_replica` (confirmed).**
   - `@lepton/omnigent` + gardener (`~/omnigent-cli/src/client.ts:52-69`): floor, crons (doctor-heal, newsletter-tick, estate-board-tick, agni-stale-red).
   - Jarvis has `SLICE_KEYS=false`; even when it's on, the shell WS and closeShell send no key.
   - The Python CLI falls back to its own machine's host id (`cli_auth.py:736-752`).
   - An empty key is round-robin in nginx 1.28 (`ngx_http_upstream_hash_module.c:566-568`).

## Major
- **Approve, interrupt and stop on the wrong replica return success and do nothing (confirmed).**
  - `_sessions/helpers.py:5655-5667,3266`; `routes_events.py:1523-1567`.
  - Fix: `_raise_if_runner_on_another_replica` in these handlers.
- **Read/unread state is in memory (confirmed).**
  - `routes_core.py:1327-1357`; `_sessions/helpers.py:558-590`.
  - Unread dots never clear, and Jarvis "Needs you" is wrong. Fix: persist it in PG.
- **The sidebar sees only sessions created on its own replica (confirmed).**
  - `runtime/user_session_stream.py` is in-process; the web never refetches while connected (`useConversations.ts:705-711`).
- **The DB pool default is 200+20 per process (confirmed).**
  - `omnigent/db/utils.py:355-356`; not overridden in env.example or compose. 2 replicas can exceed `max_connections=100`.
- **Tunnels and streams can get stuck on the wrong replica after the ring changes (suspected).**
  - `resolve` rebuilds the ring without closing open connections, and the README's paths do no `nginx -s reload`.
  - Silent chat: an SSE stream on the old replica gets heartbeats only.
  - `max_fails=0` + `proxy_next_upstream off` give hard 502s at boot.
- **Migrations have no lock (confirmed).**
  - `utils.py:478-481`. Both replicas migrate concurrently after a daemon restart.

## Minor
- **Pending-count mirror (suspected):** its dedupe cache is never cleared on handoff (`session_live_state.py:262`).
- **Duplicate sandbox work:** the reaper and keepalive run on every replica.
- **Rate limiting:** the OAuth limiter is per process.
- **Presence:** tracked per replica.
- **Hot hosts:** one host can't use more than one replica's capacity.
- **Fine as built:** the `/data` writes are atomic.

## Appendix: client audit (second reviewer), checked 2026-10-22
- **Blocker 1 re-checked by the lead and CONFIRMED.**
  - All three `wrong_replica` retry branches in `web/src/lib/identity.ts` are gated on `isDatabricksWorkspace()` (= `VITE_DATABRICKS_WORKSPACE=true`, `host.ts:233-235`).
  - The retry drops the key, which round-robins behind our router; it does not re-address.
  - The client audit's claim that the bundle "re-addresses" is wrong for our build.
- **The 400 body:** `{"error":{"code":"wrong_replica"}}` (`server/app.py:2401-2404`). The terminal-attach WS closes with 4400 (`terminals/ws_common.py:28`).
- **Routes that can return it:**
  - runner routing: `runner/routing.py:250,286,382`
  - `POST /sessions` with `host_id`: `_session_create_validation.py:387`
  - SSE `/sessions/{id}/stream`: `routes_events.py:3165`
  - `/hosts/{id}/worktrees|filesystem|model-options`: `routes/hosts.py:810,1352,1790`
- **Safe on any replica:** snapshot `runner_online` (DB fallback), `WS /sessions/updates`, list reads.
- **Keyless callers that write:**
  - **gardener** (`~/omnigent-cli/src/client.ts:52-69`): events, create, doctor `--fix`, worktrees, model-options, fork.
  - **`@lepton/omnigent`** (`NAP web-modules/omnigent/src/client.ts:56-95`), and through it:
    - the floor CLI;
    - the omni console SSE and events proxies;
    - **the network-access product AI** (`apps/network-access/src/server/ai/omnigent.ts:601-605,661`). User-facing turns would fail (N-1)/N of the time.
  - **Crons:**
    - doctor-heal: send fails silently, retried after 30 min;
    - newsletter-tick and estate-board-tick: fall through to creating a new session;
    - agni-stale-red: the create fails.
  - **gardener and floor doctor's worktree probe:** it swallows `wrong_replica` (`cli.ts:758-762,868`), so it may revive the wrong session.
  - **Jarvis:** `SLICE_KEYS=false` (`omnigent.ts:46`).
    - Even when it's on, these stay keyless: the terminal attach WS (`api/shell/ws.ts:52-55`), `closeShell` (`omni-write.ts:290`), and sessions missing from the hub rows (`hub.ts:1106`).
    - It never retries.
- **Mobile:** WebView shells; the only native call is `/v1/me` (safe).
