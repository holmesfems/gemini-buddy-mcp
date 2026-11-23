"""A stand-in for `agy acp` in tests: speaks just enough ACP over stdio.

FAKE_ACP_MODE picks the behaviour:
  ok      answer every prompt (asks for one permission first if the prompt contains "PERMISSION")
  noauth  fail `authenticate`
  hang    never answer session/prompt (until session/cancel)
  crash   exit in the middle of a prompt
  junk    send malformed messages before answering a prompt
  nosession  fail session/new
  nocheck read the isolation check file without asking (as if agy ignored --cli_dir_name)
Delegated tasks (gemini_delegate_*) act on markers in the prompt, asking permission first like agy
in strict mode and doing the action only when allowed: EDIT_INSIDE (fix a.py), EDIT_OUTSIDE
(write ../outside.txt), READ_GIT, RUN:<command up to end of line> (writes ran.txt), SUBAGENT,
SNEAKY (an edit reported done without asking). A denied action ends the turn, as in agy.
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
cwds: dict = {}


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


def act(sid, name, kind, raw, path=None):
    """One tool call in agy's order: announce, ask, then complete or fail. True if allowed."""
    next_id[0] += 1
    rid, cid = next_id[0], f"call_{next_id[0]}"
    call = {"toolCallId": cid, "name": name, "kind": kind, "title": str(raw)[:80], "rawInput": raw}
    if path:
        call["locations"] = [{"path": path}]
    update(sid, {"sessionUpdate": "tool_call", **call, "status": "in_progress"})
    ev = waiting[rid] = threading.Event()
    send({"jsonrpc": "2.0", "id": rid, "method": "session/request_permission", "params": {
        "sessionId": sid, "toolCall": {**call, "status": "pending"},
        "options": [{"optionId": "allow", "kind": "allow_once", "name": "Allow"},
                    {"optionId": "deny", "kind": "reject_once", "name": "Deny"}]}})
    ev.wait(10)
    ok = ((replies.get(rid) or {}).get("result") or {}).get("outcome", {}).get("optionId") == "allow"
    update(sid, {"sessionUpdate": "tool_call_update", "toolCallId": cid, "name": name, "kind": kind,
                 "status": "completed" if ok else "failed"})
    return ok


def delegate_turn(msg, sid, text):
    cwd = cwds.get(sid, ".")
    end = lambda words: (update(sid, {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": words}}),  # noqa: E731
                         send({"jsonrpc": "2.0", "id": msg["id"], "result": {"stopReason": "end_turn"}}))
    if text.startswith("The permission policy denied"):
        return end("continued without it")
    if "SNEAKY" in text:
        update(sid, {"sessionUpdate": "tool_call", "toolCallId": "sneak", "name": "write_file", "kind": "edit",
                     "title": "x", "status": "completed"})
        cancelled.wait(30)
        return
    if "HANG" in text:
        cancelled.wait(30)
        return end("woke up")
    if "SUBAGENT" in text:
        update(sid, {"sessionUpdate": "tool_call", "toolCallId": "sub", "name": "invoke_subagent", "kind": "other",
                     "title": "Run invoke_subagent", "status": "in_progress"})
        cancelled.wait(30)
        return
    steps = []
    if "READ_GIT" in text:
        steps.append(("read_file", "read", {"Path": os.path.join(cwd, ".git")}, None))
    if "EDIT_OUTSIDE" in text:
        out = os.path.join(cwd, "..", "outside.txt")
        steps.append(("write_file", "edit", {"Path": out, "Content": "hi"}, lambda: open(out, "w").write("hi")))
    if "EDIT_INSIDE" in text:
        a = os.path.join(cwd, "a.py")
        steps.append(("edit_file", "edit", {"Path": a}, lambda: open(a, "w").write("def add(a, b):\n    return a + b\n")))
    for line in text.splitlines():
        if line.startswith("RUN:"):
            ran = os.path.join(cwd, "ran.txt")
            steps.append(("run_command", "execute", {"CommandLine": line[4:].strip(), "WaitMsBeforeAsync": 5000},
                          lambda: open(ran, "w").write("1")))
    for name, kind, raw, do in steps:
        if not act(sid, name, kind, raw, raw.get("Path")):
            return end("")   # agy ends its turn on a rejection
        if do:
            do()
    end("I fixed a.py." if "EDIT_INSIDE" in text else "done")


def check_turn(msg, sid, text):
    path = text.split("Read the file ", 1)[1].split(" with your", 1)[0]
    if MODE == "nocheck":
        update(sid, {"sessionUpdate": "tool_call", "toolCallId": "c1", "name": "read_file", "kind": "read",
                     "title": "File check.txt", "status": "completed"})
        update(sid, {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": open(path).read()}})
    else:
        act(sid, "read_file", "read", {"Path": path}, path)
    send({"jsonrpc": "2.0", "id": msg["id"], "result": {"stopReason": "end_turn"}})


def handle_prompt(msg):
    p = msg["params"]
    sid = p["sessionId"]
    text = p["prompt"][0]["text"]
    if "Read the file " in text and "check.txt" in text:
        return check_turn(msg, sid, text)
    if ("You are doing a task in a git worktree" in text or text.startswith("The permission policy denied")
            or text.startswith("REPLY")):
        return delegate_turn(msg, sid, text)
    if MODE == "hang":
        cancelled.wait(30)
        send({"jsonrpc": "2.0", "id": msg["id"], "result": {"stopReason": "cancelled"}})
        return
    if MODE == "crash":
        os._exit(3)
    if MODE == "junk":
        send({"jsonrpc": "2.0", "method": "session/update", "params": [1, 2]})
        send({"jsonrpc": "2.0", "method": "session/update", "params": {"sessionId": sid, "update": "x"}})
        send({"jsonrpc": "2.0", "id": {"odd": 1}, "result": 5})
    if "PERMISSION" in text:
        r = ask_permission(sid)
        chosen = ((r or {}).get("result") or {}).get("outcome", {}).get("optionId")
        update(sid, {"sessionUpdate": "agent_message_chunk",
                     "content": {"type": "text", "text": f"permission={chosen} "}})
    update(sid, {"sessionUpdate": "tool_call", "name": "read_file", "kind": "read", "title": "Read x"})
    if "Task: review" in text:
        answer = ("Here is my review.\n```json\n" + json.dumps({"findings": [{
            "file": "a.py", "line": 2, "severity": "high", "category": "correctness",
            "summary": "引き算になっている", "failure_scenario": "add(1, 2) が -1", "confidence": 0.9}],
            "notes": "ほかは問題なし"}, ensure_ascii=False) + "\n```"
                  if "PROSE" not in text else "a.py の2行目が引き算になっています。")
        update(sid, {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": answer}})
        send({"jsonrpc": "2.0", "id": msg["id"], "result": {"stopReason": "end_turn"}})
        return
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
    elif method == "session/new" and MODE == "nosession":
        send({"jsonrpc": "2.0", "id": rid, "error": {"code": -32000, "message": "no workspace"}})
    elif method == "session/new":
        sessions += 1
        cwds[f"0000000{sessions}-aaaa-bbbb-cccc-dddddddddddd"] = msg["params"].get("cwd")
        send({"jsonrpc": "2.0", "id": rid, "result": {"sessionId": f"0000000{sessions}-aaaa-bbbb-cccc-dddddddddddd"}})
    elif method == "session/prompt":
        threading.Thread(target=handle_prompt, args=(msg,), daemon=True).start()
    elif method == "session/cancel":
        cancelled.set()
    elif rid is not None:  # set_mode, set_model, close
        send({"jsonrpc": "2.0", "id": rid, "result": {}})
