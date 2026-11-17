# Multi-replica live rehearsal (3-4 replicas, scratch PG max_connections=100), 2026-10-22
Evidence (scripts, raw logs, router and replica logs): `mrqa-scripts/` (untracked). Baseline `verify.py` PASS with 3 replicas, also behind a prod-style nginx.

## Blockers
1. **Scheduled tasks (S7)**
   - A task created on a non-scheduler replica never fires, with no error. After a replica-0 restart, the fire fails with `host_offline` because the tunnel is elsewhere.
   - Net effect: it works for about 1/N of hosts. For hosts on replica-0 it fires exactly once.
2. **PG connection exhaustion (S5)**
   - The pool is 200+20 per replica.
   - With 4 replicas: "too many clients", 39 of ~1,100 requests returned 500 at 100 concurrent clients.
   - Idle pooled connections stay held, which blocks psql, backups, migrations and new replicas.
   - 3 replicas peaked at 86 connections.
   - Throughput was flat at ~45 rps with 3 or 4 replicas. **Adding replicas bought nothing.**
3. **Elicitation answered through the wrong replica (S8)**
   - It returns 202 and does nothing; the GET there says "resolved" with 0 pending, and the agent keeps waiting.
   - Keyless clients (Jarvis, Slack door, scripts) lose about 2/3 of approvals.

## Major
4. **Router reload drops everyone (S6).** Any `nginx -s reload` drops ALL SSE, terminal, sidebar and host/runner sockets within 2 s, not about 1/N.
5. **Replies get cut (S6).** When the runner tunnel drops mid-turn, the saved reply loses its first ~10 words (6/6 sessions). A replica death also saves a full duplicate later, sometimes after the user's next message. Probably a pre-existing bug, made routine.
6. **A dead replica is not routed around (S6).** Its hosts hang ~45 s, then 502 until a restart (`docker kill` is not auto-restarted). Unkeyed requests 502 1/N of the time (10/40).
7. **Keyless clients (S2).** `POST /events`, streams and approvals succeed 1/N of the time; otherwise a clean `400 wrong_replica`, which no Python client handles. Runners stamp their own host id, so cross-host session calls miss.
8. **The web client never retries `wrong_replica`.** The retry is gated on `isDatabricksWorkspace()`. A host without the key: 100% 400s in the browser.

## Minor
- **Sidebar:** new sessions show only on the creating replica's sockets; status reaches all replicas in ~1 s.
- **`upstreams.conf`:** a `sed -i` edit is invisible to the router (single-file bind mount inode).
- **README rollback order:** it leaves a 502 window; reverse it.
- **Router start:** it starts before the replicas are healthy, so 502s at boot.
- **Header leak:** `X-Omnigent-Upstream` exposes internal IPs.
- **Stray 502s:** 2 random 502s under load, not reproduced.
- **Empty key:** spread evenly (100/100/100).
