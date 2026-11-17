"""Concurrent unkeyed read load: list sessions / get session / hosts. Usage: load.py CONC SECONDS [session_id...]"""
import asyncio, sys, time, collections, random
sys.path.insert(0, "/tmp/mrqa-tools")
import mr

async def main(conc, secs, sids):
    stats = collections.Counter(); lat = []; errs = collections.Counter(); ups = collections.Counter()
    end = time.monotonic() + secs
    limits = mr.httpx.Limits(max_connections=conc * 2, max_keepalive_connections=conc * 2)
    async with mr.client(timeout=60, limits=limits) as c:
        async def worker(i):
            while time.monotonic() < end:
                k = random.random()
                if k < 0.5:
                    req = c.get("/v1/sessions", headers=mr.H, params={"limit": 50, "visibility": "all"})
                elif k < 0.8 and sids:
                    req = c.get(f"/v1/sessions/{random.choice(sids)}", headers=mr.H)
                else:
                    req = c.get("/v1/hosts", headers=mr.H)
                t = time.monotonic()
                try:
                    r = await req
                    stats[r.status_code] += 1; ups[r.headers.get(mr.UP, "?")] += 1
                    if r.status_code >= 400: errs[r.text[:120]] += 1
                except Exception as e:
                    stats[type(e).__name__] += 1
                lat.append(time.monotonic() - t)
        await asyncio.gather(*(worker(i) for i in range(conc)))
    lat.sort(); n = len(lat)
    print(f"conc={conc} reqs={n} rps={n/secs:.1f} p50={lat[n//2]*1000:.0f}ms p95={lat[int(n*.95)]*1000:.0f}ms p99={lat[int(n*.99)]*1000:.0f}ms status={dict(stats)} upstreams={dict(ups)}")
    for e, cnt in errs.most_common(5): print("  err", cnt, e)

asyncio.run(main(int(sys.argv[1]), float(sys.argv[2]), sys.argv[3:]))
