import asyncio, sys, json, datetime
sys.path.insert(0, "/tmp/mrqa-tools"); import mr
async def main():
    t = json.load(open("/tmp/mrqa-logs/sched_tasks.json")); a = json.load(open("/tmp/mrqa-logs/hostA.json"))
    m = (datetime.datetime.now(datetime.UTC) + datetime.timedelta(minutes=2)).minute
    async with mr.client() as c:
        r = await c.patch(f"/v1/scheduled-tasks/{t['r1']}", headers={**mr.H, mr.KEY: a["host_id"]}, json={"rrule": f"FREQ=HOURLY;BYMINUTE={m};BYSECOND=0"})
        print("update r1 task", r.status_code, r.headers.get(mr.UP), "minute", m, r.text[:150])
asyncio.run(main())
