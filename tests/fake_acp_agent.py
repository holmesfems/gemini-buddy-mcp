"""A stand-in for `agy acp` in tests: speaks just enough ACP over stdio.

FAKE_ACP_MODE picks the behaviour:
  ok      answer every prompt (asks for one permission first if the prompt contains "PERMISSION")
  noauth  fail `authenticate`
  hang    never answer session/prompt (until session/cancel)
  crash   exit in the middle of a prompt
Every request received is appended to FAKE_ACP_LOG (one JSON per line) when set.
"""
import json
import os
import sys
import threading

MODE = os.environ.get("FAKE_ACP_MODE", "ok")
LOG = os.environ.get("FAKE_ACP_LOG")
out_lock = threading.Lock()
waiting: dict = {}       # our request id -> Event, for replies from the client
replies: dict = {}
cancelled = threading.Event()
next_id = [1000]


def send(obj):
    with out_lock:   # UTF-8 like agy acp, whatever the console code page is
        sys.stdout.buffer.write((json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8"))
        sys.stdout.buffer.flush()


def update(sid, upd):
    send({"jsonrpc": "2.0", "method": "session/update", "params": {"sessionId": sid, "update": upd}})


def ask_permission(sid):
    next_id[0] += 1
    rid = next_id[0]
    ev = waiting[rid] = threading.Event()
    send({"jsonrpc": "2.0", "id": rid, "method": "session/request_permission", "params": {
        "sessionId": sid,
        "toolCall": {"toolCallId": "t1", "name": "run_command", "kind": "execute", "title": "echo hi"},
        "options": [{"optionId": "a", "kind": "allow_once", "name": "Allow"},
                    {"optionId": "r", "kind": "reject_once", "name": "Reject"}]}})
    ev.wait(10)
    return replies.get(rid)


def handle_prompt(msg):
    p = msg["params"]
    sid = p["sessionId"]
    text = p["prompt"][0]["text"]
    if MODE == "hang":
        cancelled.wait(30)
        send({"jsonrpc": "2.0", "id": msg["id"], "result": {"stopReason": "cancelled"}})
        return
    if MODE == "crash":
        os._exit(3)
    if "PERMISSION" in text:
        r = ask_permission(sid)
        chosen = ((r or {}).get("result") or {}).get("outcome", {}).get("optionId")
        update(sid, {"sessionUpdate": "agent_message_chunk",
                     "content": {"type": "text", "text": f"permission={chosen} "}})
    update(sid, {"sessionUpdate": "tool_call", "name": "read_file", "kind": "read", "title": "Read x"})
    for part in ("こんにちは", "、", sid[:8]):
        update(sid, {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": part}})
    send({"jsonrpc": "2.0", "id": msg["id"], "result": {"stopReason": "end_turn", "usage": {"totalTokens": 7}}})


sessions = 0
for line in sys.stdin.buffer:
    msg = json.loads(line.decode("utf-8"))
    if LOG:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(msg, ensure_ascii=False) + "\n")
    method = msg.get("method")
    if method is None:  # a reply to our permission request
        replies[msg["id"]] = msg
        if msg["id"] in waiting:
            waiting[msg["id"]].set()
        continue
    rid = msg.get("id")
    if method == "initialize":
        send({"jsonrpc": "2.0", "id": rid, "result": {
            "protocolVersion": 1, "agentInfo": {"name": "fake", "version": "0"},
            "authMethods": [{"id": "oauth-personal"}, {"id": "gemini-api-key"}]}})
    elif method == "authenticate":
        if MODE == "noauth":
            send({"jsonrpc": "2.0", "id": rid, "error": {"code": -32000, "message": "not logged in"}})
        else:
            send({"jsonrpc": "2.0", "id": rid, "result": {
                "_meta": {"antigravity": {"tier": {"name": "Fake Pro"}}}}})
    elif method == "session/new":
        sessions += 1
        send({"jsonrpc": "2.0", "id": rid, "result": {"sessionId": f"0000000{sessions}-aaaa-bbbb-cccc-dddddddddddd"}})
    elif method == "session/prompt":
        threading.Thread(target=handle_prompt, args=(msg,), daemon=True).start()
    elif method == "session/cancel":
        cancelled.set()
    elif rid is not None:  # set_mode, set_model, close
        send({"jsonrpc": "2.0", "id": rid, "result": {}})
