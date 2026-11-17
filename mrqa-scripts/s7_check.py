import asyncio, sys, json
sys.path.insert(0, "/tmp/mrqa-tools"); import mr
async def main():
    t = json.load(open("/tmp/mrqa-logs/sched_tasks.json"))
    async with mr.client() as c:
        for n, tid in t.items():
            r = await c.get(f"/v1/scheduled-tasks/{tid}/runs", headers=mr.H)
            runs = r.json().get("data", r.json()) if r.is_success else r.text
            print(n, tid, r.status_code, json.dumps(runs)[:400])
asyncio.run(main())
