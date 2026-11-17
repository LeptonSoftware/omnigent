import asyncio, sys, collections, time
sys.path.insert(0, "/tmp/mrqa-tools"); import mr
async def main(gap):
    cnt = collections.Counter()
    async with mr.client() as c:
        for i in range(12):
            rs = await asyncio.gather(*(c.get("/v1/hosts", headers=mr.H) for _ in range(30)), return_exceptions=True)
            for r in rs: cnt[getattr(r, "status_code", type(r).__name__)] += 1
            await asyncio.sleep(gap)
    print(f"gap={gap}s", dict(cnt))
for g in (float(x) for x in sys.argv[1:]): asyncio.run(main(g))
