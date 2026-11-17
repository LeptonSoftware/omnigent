import asyncio, sys, json, time
sys.path.insert(0, "/tmp/mrqa-tools"); import mr
REPS = {"r0": "192.168.16.3:8000", "r1": "192.168.16.4:8000", "r2": "192.168.16.5:8000"}
async def main():
    a = json.load(open("/tmp/mrqa-logs/hostA.json")); hid = a["host_id"]
    async with mr.client() as c:
        keys = {n: await mr.key_for(c, up) for n, up in REPS.items()}
        sid, _, _ = await mr.make_session(c, hid, mr.Path(a["scratch"]) / "ws", f"mrqa-ask-{hid[:6]}", ask_policy=True, title="mrqa-elicit")
        print("session", sid)
        r = await c.post(f"/v1/sessions/{sid}/events", headers={**mr.H, mr.KEY: hid}, json=mr.msg("tool please"))
        print("post", r.status_code)
        async def pending():
            s = (await c.get(f"/v1/sessions/{sid}", headers={**mr.H, mr.KEY: hid})).json()
            return s.get("pending_elicitations") or None
        try:
            pe = await mr.wait(pending, timeout=60)
        except TimeoutError:
            s = (await c.get(f"/v1/sessions/{sid}", headers={**mr.H, mr.KEY: hid})).json()
            print("NO ELICITATION; status", s.get("status"), json.dumps(s)[:800]); return
        eid = pe[0].get("elicitation_id") or pe[0].get("id") or pe[0].get("params", {}).get("elicitation_id")
        print("pending on owner:", json.dumps(pe)[:300]); print("eid", eid)
        for n in REPS:
            s = await c.get(f"/v1/sessions/{sid}", headers={**mr.H, mr.KEY: keys[n]})
            g = await c.get(f"/v1/sessions/{sid}/elicitations/{eid}", headers={**mr.H, mr.KEY: keys[n]})
            print(f"  via {n}: GET session {s.status_code} pending={len((s.json() or {}).get('pending_elicitations') or []) if s.is_success else s.text[:100]} | GET elicitation {g.status_code} {g.text[:120]}")
        for n in ("r0", "r2"):
            r = await c.post(f"/v1/sessions/{sid}/elicitations/{eid}/resolve", headers={**mr.H, mr.KEY: keys[n]}, json={"action": "accept"})
            print(f"  resolve via {n}: {r.status_code} {r.text[:200]}")
            r = await c.post(f"/v1/sessions/{sid}/events", headers={**mr.H, mr.KEY: keys[n]}, json={"type": "approval", "data": {"elicitation_id": eid, "action": "accept"}})
            print(f"  approval event via {n}: {r.status_code} {r.text[:200]}")
        s = (await c.get(f"/v1/sessions/{sid}", headers={**mr.H, mr.KEY: hid})).json()
        print("after wrong-replica attempts: still pending on owner =", bool(s.get("pending_elicitations")), "status", s.get("status"))
        r = await c.post(f"/v1/sessions/{sid}/elicitations/{eid}/resolve", headers={**mr.H, mr.KEY: hid}, json={"action": "accept"})
        print("  resolve via owner:", r.status_code, r.text[:200])
        async def done():
            it = await c.get(f"/v1/sessions/{sid}/items", headers=mr.H, params={"limit": 20, "order": "desc"})
            return "tool done" in it.text
        try: await mr.wait(done, timeout=40); print("turn completed after owner resolve")
        except TimeoutError: print("turn did NOT complete after owner resolve")
asyncio.run(main())
