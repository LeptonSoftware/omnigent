import asyncio, sys, collections
sys.path.insert(0, "/tmp/mrqa-tools"); import mr
async def main():
    async with mr.client() as c:
        for label, kw in [("no key", {}), ("empty header", {"headers": {mr.KEY: ""}}), ("empty query", {"params": {"omnigent_slice_key": ""}}),
                          ("const 'x'", {"headers": {mr.KEY: "x"}})]:
            cnt = collections.Counter()
            for _ in range(300):
                r = await c.get("/health", **kw); cnt[r.headers.get(mr.UP)] += 1
            print(f"{label:14s}", dict(sorted(cnt.items())))
        # concurrent burst
        cnt = collections.Counter()
        rs = await asyncio.gather(*(c.get("/v1/hosts", headers=mr.H) for _ in range(300)))
        for r in rs: cnt[r.headers.get(mr.UP)] += 1
        print("concurrent unkeyed /v1/hosts x300", dict(sorted(cnt.items())))
asyncio.run(main())
