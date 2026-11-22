"""Talking to `agy acp` (Agent Client Protocol: JSON-RPC 2.0, one JSON object per line on stdio).

One `agy acp` process is kept running and shared by every call; each call opens its own ACP
session, sends one prompt, collects the streamed answer and closes the session. Compared with a
fresh `agy -p` per call this skips agy's start-up, and lets this server answer agy's permission
requests itself instead of relying on headless mode refusing them.

Sign-in: `authenticate` with `oauth-personal` reuses the Google login agy already holds (the
subscription). The API-key method is never used, and the child gets no API keys in its env.

Permissions: sessions run in agy's plan mode, where agy does not run commands or edit files,
and every permission request agy still sends is rejected. Rejected requests are returned as
`denied_actions`, like the CLI's field of the same name. (Outside plan mode agy runs commands
its own settings allow, e.g. "proceed-in-sandbox", without asking: keep sessions in plan mode.)
"""
from __future__ import annotations

import atexit
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable

from .agyproc import decode
from .errors import Fail

PROTOCOL_VERSION = 1
AUTH_METHOD = "oauth-personal"
HANDSHAKE_TIMEOUT = 60.0
RPC_TIMEOUT = 60.0
CANCEL_GRACE = 15.0   # seconds to wait for the prompt to end after session/cancel
LOGIN_HINT = ("agy is not signed in for ACP. Run `agy acp --login` once in a terminal "
              "(it opens a Google sign-in), then call again.")


class RpcError(Exception):
    def __init__(self, method: str, error: dict):
        self.method, self.error = method, error
        super().__init__(f"{method}: {error.get('message') or error}")


class _Pending:
    __slots__ = ("event", "msg")

    def __init__(self):
        self.event = threading.Event()
        self.msg: dict | None = None


class _Turn:
    """What one session streamed back during a prompt."""

    def __init__(self):
        self.chunks: list[str] = []
        self.denied: list[dict] = []
        self.tools: list[dict] = []

    def on_update(self, update: dict) -> None:
        kind = update.get("sessionUpdate")
        if kind == "agent_message_chunk":
            content = update.get("content") or {}
            if content.get("type") == "text" and isinstance(content.get("text"), str):
                self.chunks.append(content["text"])
        elif kind == "tool_call":
            self.tools.append({"tool": update.get("name"), "kind": update.get("kind"),
                               "title": update.get("title")})

    def on_permission(self, params: dict) -> dict:
        """Reject: read-only sessions approve nothing."""
        call = params.get("toolCall") or {}
        self.denied.append({"tool": call.get("name"), "kind": call.get("kind"), "title": call.get("title")})
        options = params.get("options") or []
        chosen = next((o for k in ("reject_once", "reject_always") for o in options if o.get("kind") == k), None)
        if chosen is None:
            return {"outcome": {"outcome": "cancelled"}}
        return {"outcome": {"outcome": "selected", "optionId": chosen.get("optionId")}}


class AcpClient:
    """One running `agy acp` process."""

    def __init__(self, exe: str, cwd: Path, env: dict[str, str], log_path: Path | None = None,
                 args: list[str] | None = None):
        self.exe, self.cwd, self.env = exe, cwd, env
        self.args = args if args is not None else ["acp"]
        self.log_path = log_path
        self.proc: subprocess.Popen | None = None
        self._ids = 0
        self._lock = threading.Lock()          # ids, pending, turns
        self._write = threading.Lock()
        self._pending: dict[int, _Pending] = {}
        self._turns: dict[str, _Turn] = {}
        self.agent_info: dict = {}
        self.tier: str | None = None

    # ------------------------------------------------------------ process

    def start(self) -> None:
        log = open(self.log_path, "ab") if self.log_path else subprocess.DEVNULL
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0
        try:
            self.proc = subprocess.Popen([self.exe, *self.args], stdin=subprocess.PIPE,
                                         stdout=subprocess.PIPE, stderr=log, env=self.env,
                                         cwd=str(self.cwd), creationflags=flags)
        except FileNotFoundError:
            raise Fail(f"agy not found ({self.exe}); install Antigravity CLI or set GEMINI_BUDDY_AGY")
        finally:
            if log is not subprocess.DEVNULL:
                log.close()   # the child keeps its own handle
        threading.Thread(target=self._read_loop, name="agy-acp-reader", daemon=True).start()
        try:
            init = self.request("initialize", {
                "protocolVersion": PROTOCOL_VERSION,
                "clientCapabilities": {"fs": {"readTextFile": False, "writeTextFile": False},
                                       "terminal": False},
                "clientInfo": {"name": "gemini-buddy-mcp", "version": "0.2.0"},
            }, HANDSHAKE_TIMEOUT)
            self.agent_info = init.get("agentInfo") or {}
            methods = [m.get("id") for m in init.get("authMethods") or []]
            if methods and AUTH_METHOD not in methods:
                raise Fail(f"agy acp offers no {AUTH_METHOD} sign-in (offers {methods})")
            try:
                auth = self.request("authenticate", {"methodId": AUTH_METHOD}, HANDSHAKE_TIMEOUT)
            except RpcError as e:
                raise Fail(f"{LOGIN_HINT} ({e})")
            tier = ((auth or {}).get("_meta") or {}).get("antigravity", {}).get("tier")
            self.tier = tier.get("name") if isinstance(tier, dict) else None
        except BaseException:
            self.close()
            raise

    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def close(self) -> None:
        if self.proc is not None and self.proc.poll() is None:
            self.proc.kill()
            try:
                self.proc.wait(5)
            except subprocess.TimeoutExpired:
                pass
        if self.proc is not None:
            for pipe in (self.proc.stdin, self.proc.stdout):
                try:
                    pipe.close()
                except (OSError, ValueError):
                    pass
        self._fail_pending("agy acp stopped")

    def _fail_pending(self, why: str) -> None:
        with self._lock:
            pending, self._pending = self._pending, {}
        for p in pending.values():
            p.msg = {"error": {"message": why}}
            p.event.set()

    # ------------------------------------------------------------ wire

    def _send(self, obj: dict) -> None:
        data = (json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8")
        with self._write:
            if not self.alive():
                raise Fail("agy acp is not running")
            try:
                self.proc.stdin.write(data)
                self.proc.stdin.flush()
            except OSError as e:
                raise Fail(f"agy acp closed its input: {e}")

    def request(self, method: str, params: dict, timeout: float = RPC_TIMEOUT) -> Any:
        with self._lock:
            self._ids += 1
            rid = self._ids
            pending = self._pending[rid] = _Pending()
        try:
            self._send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
            if not pending.event.wait(timeout):
                raise TimeoutError(f"{method} got no reply within {timeout:.0f} s")
        finally:
            with self._lock:
                self._pending.pop(rid, None)
        msg = pending.msg or {}
        if "error" in msg:
            raise RpcError(method, msg["error"] if isinstance(msg["error"], dict) else {"message": msg["error"]})
        return msg.get("result")

    def notify(self, method: str, params: dict) -> None:
        self._send({"jsonrpc": "2.0", "method": method, "params": params})

    def _read_loop(self) -> None:
        try:
            for line in self.proc.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(decode(line))   # UTF-8 (seen), console code page as a fallback
                except ValueError:
                    continue   # not ours (a stray log line)
                if isinstance(msg, dict):
                    self._dispatch(msg)
        finally:
            self._fail_pending("agy acp exited")

    def _dispatch(self, msg: dict) -> None:
        method = msg.get("method")
        if method is None:  # a reply to one of our requests
            with self._lock:
                pending = self._pending.get(msg.get("id"))
            if pending:
                pending.msg = msg
                pending.event.set()
            return
        params = msg.get("params") or {}
        with self._lock:
            turn = self._turns.get(params.get("sessionId"))
        if "id" not in msg:  # a notification
            if method == "session/update" and turn:
                turn.on_update(params.get("update") or {})
            return
        # a request from agy: answer it, never leave agy waiting
        try:
            if method == "session/request_permission":
                result = turn.on_permission(params) if turn else {"outcome": {"outcome": "cancelled"}}
                self._send({"jsonrpc": "2.0", "id": msg["id"], "result": result})
            else:  # fs/* and terminal/*: we declared no such capability
                self._send({"jsonrpc": "2.0", "id": msg["id"],
                            "error": {"code": -32601, "message": f"{method} is not supported"}})
        except Fail:
            pass

    # ------------------------------------------------------------ one prompt

    def prompt(self, text: str, model: str, cwd: Path, dirs: list[Path], timeout: float) -> dict:
        """One read-only prompt in a fresh plan-mode session (closed afterwards)."""
        t0 = time.time()
        params: dict[str, Any] = {"cwd": str(cwd), "mcpServers": []}
        extra = list(dict.fromkeys(str(d) for d in dirs))
        if extra:
            params["additionalDirectories"] = extra
        session = self.request("session/new", params)
        sid = session["sessionId"]
        turn = _Turn()
        with self._lock:
            self._turns[sid] = turn
        try:
            self.request("session/set_mode", {"sessionId": sid, "modeId": "plan"})
            self.request("session/set_model", {"sessionId": sid, "modelId": model})
            try:
                result = self.request("session/prompt", {
                    "sessionId": sid, "prompt": [{"type": "text", "text": text}]}, timeout)
            except TimeoutError:
                self.notify("session/cancel", {"sessionId": sid})
                raise Fail(f"agy did not finish within {timeout:.0f} s (cancelled)")
        except RpcError as e:
            raise Fail(f"agy acp failed: {e}")
        finally:
            with self._lock:
                self._turns.pop(sid, None)
            try:
                self.request("session/close", {"sessionId": sid}, 10)
            except Exception:  # noqa: BLE001 - closing is best effort
                pass
        return {
            "response": "".join(turn.chunks).strip(),
            "conversation_id": sid,
            "stop_reason": (result or {}).get("stopReason"),
            "seconds": round(time.time() - t0, 1),
            "denied_actions": turn.denied,
            "tools": turn.tools,
            "usage": (result or {}).get("usage"),
            "started": t0,
        }


# ---------------------------------------------------------------- shared client

_client: AcpClient | None = None
_client_lock = threading.Lock()


def shared(make: Callable[[], AcpClient]) -> AcpClient:
    """The running client, started (or restarted after a crash) on first use."""
    global _client
    with _client_lock:
        if _client is None or not _client.alive():
            if _client is not None:
                _client.close()   # the dead one: release its pipes
            c = make()
            c.start()
            _client = c
        return _client


def reset() -> None:
    global _client
    with _client_lock:
        if _client is not None:
            _client.close()
        _client = None


atexit.register(reset)   # do not leave `agy acp` running after the server exits


LOG_MAX_BYTES = 5 * 1024 * 1024


def default_log_path(home: Path) -> Path:
    """agy's stderr goes here; started afresh once it passes LOG_MAX_BYTES."""
    home.mkdir(parents=True, exist_ok=True)
    path = home / "agy-acp.log"
    try:
        if path.stat().st_size > LOG_MAX_BYTES:
            path.replace(path.with_suffix(".log.1"))
    except OSError:
        pass
    return path


def env_backend() -> str:
    return (os.environ.get("GEMINI_BUDDY_AGY_BACKEND") or "acp").strip().lower()
