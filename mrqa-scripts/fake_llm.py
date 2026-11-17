"""Fake OpenAI chat/completions server for mrqa. Behaviour keyed off the last user message:
  'slow N'  -> stream N words, 1 per second
  'tool'    -> call the first tool offered (once), then answer 'tool done'
  anything else -> 'pong'
"""
import asyncio, json, sys, time, uuid
from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse, JSONResponse
import uvicorn

app = FastAPI()
LOG = open("/tmp/mrqa-logs/fake_llm.log", "a", buffering=1)

def last_user(messages):
    for m in reversed(messages):
        if m.get("role") == "user":
            c = m.get("content")
            if isinstance(c, list):
                c = " ".join(p.get("text", "") for p in c if isinstance(p, dict))
            return c or ""
    return ""

def chunk(cid, delta, finish=None):
    return "data: " + json.dumps({"id": cid, "object": "chat.completion.chunk", "created": int(time.time()),
        "model": "fake", "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}) + "\n\n"

@app.post("/v1/chat/completions")
async def cc(req: Request):
    body = await req.json()
    msgs = body.get("messages", [])
    text = last_user(msgs)
    has_tool_result = any(m.get("role") == "tool" for m in msgs[-3:])
    tools = body.get("tools") or []
    LOG.write(f"{time.strftime('%H:%M:%S')} cc stream={body.get('stream')} last_user={text[:60]!r} tools={[t.get('function',{}).get('name') for t in tools][:8]} tool_result={has_tool_result}\n")
    cid = "chatcmpl-" + uuid.uuid4().hex[:8]
    async def gen():
        yield chunk(cid, {"role": "assistant", "content": ""})
        words = text.split()
        if words[:1] == ["tool"] and tools and not has_tool_result:
            name = next((t["function"]["name"] for t in tools if "shell" in t["function"]["name"]), tools[0]["function"]["name"])
            args = json.dumps({"command": "echo mrqa-tool-ran"})
            yield chunk(cid, {"tool_calls": [{"index": 0, "id": "call_" + uuid.uuid4().hex[:8], "type": "function",
                "function": {"name": name, "arguments": args}}]})
            yield chunk(cid, {}, "tool_calls")
        elif words[:1] == ["slow"]:
            n = int(words[1]) if len(words) > 1 else 10
            for i in range(n):
                yield chunk(cid, {"content": f"w{i} "})
                await asyncio.sleep(1)
            yield chunk(cid, {}, "stop")
        else:
            yield chunk(cid, {"content": "tool done" if has_tool_result else "pong"})
            yield chunk(cid, {}, "stop")
        yield "data: [DONE]\n\n"
    if body.get("stream"):
        return StreamingResponse(gen(), media_type="text/event-stream")
    return JSONResponse({"id": cid, "object": "chat.completion", "model": "fake", "choices": [{"index": 0,
        "message": {"role": "assistant", "content": "pong"}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}})


def _resp(rid, status, output, model="fake"):
    return {"id": rid, "object": "response", "created_at": int(time.time()), "status": status, "model": model,
            "output": output, "parallel_tool_calls": True, "tool_choice": "auto", "tools": [],
            "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2,
                      "input_tokens_details": {"cached_tokens": 0}, "output_tokens_details": {"reasoning_tokens": 0}}}

@app.post("/v1/responses")
async def responses(req: Request):
    body = await req.json()
    inp = body.get("input") or []
    if isinstance(inp, str): inp = [{"role": "user", "content": inp}]
    text = ""; 
    for it in reversed(inp):
        if it.get("role") == "user":
            c = it.get("content"); text = c if isinstance(c, str) else " ".join(x.get("text", "") for x in c if isinstance(x, dict)); break
    last_types = [it.get("type") for it in inp[-3:]]
    has_tool_result = "function_call_output" in last_types
    tools = [t.get("name") for t in (body.get("tools") or []) if t.get("type") == "function"]
    LOG.write(f"{time.strftime('%H:%M:%S')} resp stream={body.get('stream')} last_user={text[:60]!r} tools={tools[:10]} tool_result={has_tool_result}\n")
    rid = "resp_" + uuid.uuid4().hex[:12]; seq = [0]
    def ev(t, **d):
        seq[0] += 1
        return f"event: {t}\ndata: " + json.dumps({"type": t, "sequence_number": seq[0], **d}) + "\n\n"
    async def gen():
        yield ev("response.created", response=_resp(rid, "in_progress", []))
        words = text.split()
        if words[:1] == ["tool"] and tools and not has_tool_result:
            name = next((t for t in tools if "shell" in t), tools[0])
            args = json.dumps({"command": "echo mrqa-tool-ran"})
            item = {"id": "fc_" + uuid.uuid4().hex[:8], "type": "function_call", "call_id": "call_" + uuid.uuid4().hex[:8],
                    "name": name, "arguments": "", "status": "in_progress"}
            yield ev("response.output_item.added", output_index=0, item=item)
            yield ev("response.function_call_arguments.delta", item_id=item["id"], output_index=0, delta=args)
            yield ev("response.function_call_arguments.done", item_id=item["id"], output_index=0, arguments=args)
            done = {**item, "arguments": args, "status": "completed"}
            yield ev("response.output_item.done", output_index=0, item=done)
            yield ev("response.completed", response=_resp(rid, "completed", [done]))
            return
        if words[:1] == ["slow"]:
            n = int(words[1]) if len(words) > 1 else 10; chunks = [f"w{i} " for i in range(n)]; delay = 1.0
        else:
            chunks = ["tool done" if has_tool_result else "pong"]; delay = 0
        mid = "msg_" + uuid.uuid4().hex[:8]
        item = {"id": mid, "type": "message", "role": "assistant", "status": "in_progress", "content": []}
        yield ev("response.output_item.added", output_index=0, item=item)
        yield ev("response.content_part.added", item_id=mid, output_index=0, content_index=0, part={"type": "output_text", "text": "", "annotations": []})
        full = ""
        for ch in chunks:
            full += ch
            yield ev("response.output_text.delta", item_id=mid, output_index=0, content_index=0, delta=ch, logprobs=[])
            if delay: await asyncio.sleep(delay)
        part = {"type": "output_text", "text": full, "annotations": []}
        yield ev("response.output_text.done", item_id=mid, output_index=0, content_index=0, text=full, logprobs=[])
        yield ev("response.content_part.done", item_id=mid, output_index=0, content_index=0, part=part)
        done = {**item, "status": "completed", "content": [part]}
        yield ev("response.output_item.done", output_index=0, item=done)
        yield ev("response.completed", response=_resp(rid, "completed", [done]))
    return StreamingResponse(gen(), media_type="text/event-stream")

@app.api_route("/{path:path}", methods=["GET", "POST"])
async def other(path: str, req: Request):
    LOG.write(f"{time.strftime('%H:%M:%S')} UNHANDLED {req.method} /{path}\n")
    return JSONResponse({"error": "unhandled"}, status_code=404)

if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=int(sys.argv[1]), log_level="warning")
