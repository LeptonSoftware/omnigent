import asyncio, sys, json
sys.path.insert(0, "/tmp/mrqa-tools"); import mr
async def main(tag, target):
    async with mr.client() as c:
        hid = await mr.key_for(c, target)
        hid, scratch, p = mr.start_host(tag, hid)
        up = await mr.host_online(c, hid, scratch / "ws")
        sid, aid, _ = await mr.make_session(c, hid, scratch / "ws", f"mrqa-{tag.lower()}-{hid[:6]}")
        json.dump({"host_id": hid, "scratch": str(scratch), "pid": p.pid, "sid": sid, "agent_id": aid, "replica": up}, open(f"/tmp/mrqa-logs/host{tag}.json", "w"))
        print(tag, hid, up)
asyncio.run(main(sys.argv[1], sys.argv[2]))
