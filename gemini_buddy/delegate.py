"""Handing work to agy (Gemini) in a git worktree: the gemini_delegate_* tools.

Each task gets its own git worktree (a new branch from `base`) and its own `agy acp` process.
Nothing is merged: when agy's turn ends, the changes are staged in the worktree and written to
changes.patch for the caller to read and take in.

Keeping agy inside the worktree (agy 1.5.14, checked 2026-11):

- Over ACP, outside plan mode, agy runs whatever its own settings.json allows without asking
  (e.g. "toolPermission": "proceed-in-sandbox"), whatever we answer. So each process is started
  with `--cli_dir_name gemini-buddy-delegate`, which makes agy read
  `<app data>/gemini-buddy-delegate/settings.json` instead of the user's. We write that file
  before every start with "toolPermission": "strict" and empty allow / deny / ask lists. In strict
  mode agy asks before every file read, edit and command, and keeps to our answer.
- Policy (Policy.decide): reads and edits only inside the worktree (not .git, not files that
  usually hold secrets). Commands only when the caller listed them (allow_commands), matched
  as a prefix, with no shell operators. Everything else is rejected.
- Check at start (canary): if a later agy ignored --cli_dir_name, the user's settings would apply
  again without any error. Before each task, agy is asked to read a check file and must ask us
  first; a read done without asking stops the task before it begins.
- Second guard: tool calls are watched. A subagent (invoke_subagent; it runs without asking,
  and keeps going after the turn ends) or a read / edit / command that finishes without our
  approval kills the process and stops the task.
- agy ends its turn when an action is rejected. The task is then nudged to go on without it, at
  most MAX_NUDGES times.
- Commands: an allowed command must stay in agy's terminal sandbox. When it fails there, agy
  asks again with BypassSandbox: true, which is refused.
- Paths given to agy: with a Windows 8.3 short name in the session's cwd (C:\\Users\\ABCDEF~1\\...),
  agy 1.5.15 reads files there without asking, even in strict mode (checked 2026-11-24; the same
  folder under its long name asks). Every cwd sent to agy is resolve()d to its long name first:
  the worktree, and the check folder (a short GEMINI_BUDDY_HOME or TEMP would otherwise fail the
  check every time).
- Not covered: search_web runs without asking (it sends a query, not files). An allowed command
  can run code that agy wrote (inside the sandbox).
"""
from __future__ import annotations

import atexit
import fnmatch
import json
import os
import re
import subprocess
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from .acp import AcpClient, RpcError
from .agyproc import agy_env, agy_exe, decode
from .errors import Fail

CLI_DIR_NAME = "gemini-buddy-delegate"
SETTINGS = {"toolPermission": "strict", "enableTerminalSandbox": True,
            "permissions": {"allow": [], "deny": [], "ask": []}}

NO_ASK_TOOLS = {"search_web"}                     # runs without asking even in strict mode
WATCHED_KINDS = {"read", "edit", "delete", "move", "execute", "fetch"}
FILE_KINDS = {"read", "edit", "delete", "move"}
SECRET_NAMES = (".env", ".env.*", "*.pem", "*.key", "id_rsa*", "id_ed25519*",
                "*credentials*.json", "secrets.*", "*.secret")
SHELL_META = re.compile(r"[;&|<>`$%^\r\n(){}]")
CONTROL = re.compile(r"[\x00-\x08\x0a-\x1f\x7f\x85  ]")   # tab is fine
PATH_KEY = re.compile(r"path|file|dir|cwd", re.I)

RUNNING = ("starting", "checking", "running")
MAX_NUDGES = 2
CHECK_TIMEOUT = 180.0
EVENTS_SHOWN = 60
TEXT_TAIL = 1500
PATCH_CHARS = 20000
LISTING_LINES = 400
STATUS_WAIT_MAX = 110.0


def timeout_seconds() -> float:
    return float(os.environ.get("GEMINI_BUDDY_DELEGATE_TIMEOUT", "1800"))


def idle_seconds() -> float:
    """How long a finished task's agy stays up for gemini_delegate_reply."""
    return float(os.environ.get("GEMINI_BUDDY_DELEGATE_IDLE", "1800"))


def now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


# ---------------------------------------------------------------- agy settings and process

def agy_app_dir() -> Path:
    env = os.environ.get("GEMINI_BUDDY_AGY_APP_DIR")
    return Path(env) if env else Path.home() / ".gemini" / "antigravity-cli"


def write_settings() -> Path:
    """The strict settings.json the delegate's agy reads (via --cli_dir_name). Rewritten every
    start, so an edit to it does not survive: this folder belongs to gemini-buddy."""
    folder = agy_app_dir() / CLI_DIR_NAME
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / "settings.json"
    text = json.dumps(SETTINGS, indent=2)
    try:
        if path.read_text(encoding="utf-8") == text:
            return path      # unchanged: do not rewrite under an agy that is reading it
    except (OSError, UnicodeDecodeError):
        pass
    tmp = folder / f"settings.{uuid.uuid4().hex[:8]}.tmp"   # a task starting at the same time
    tmp.write_text(text, encoding="utf-8")                  # never sees a half-written file
    os.replace(tmp, path)
    return path


def agy_args() -> list[str]:
    return ["acp", "--cli_dir_name", CLI_DIR_NAME]


def default_client(cwd: Path, log: Path | None) -> AcpClient:
    return AcpClient(agy_exe(), cwd, agy_env(), log, args=agy_args())


# ---------------------------------------------------------------- policy

def plain_command(cmd: str) -> str | None:
    """`cmd` with runs of spaces collapsed, or None if it has shell operators or control characters.
    Checked on the raw text: collapsing first would turn a newline (a second command) into a space."""
    cmd = cmd or ""
    if SHELL_META.search(cmd) or CONTROL.search(cmd):
        return None
    return " ".join(cmd.split()) or None


def command_allowed(cmd: str, allowed: list[str]) -> bool:
    cmd = plain_command(cmd)
    return cmd is not None and any(cmd == a or cmd.startswith(a + " ") for a in allowed)


def check_commands(allowed: list[str] | None) -> list[str]:
    out = []
    for a in allowed or []:
        plain = plain_command(a)
        if plain is None:
            raise Fail(f"allow_commands entries must be plain commands without shell operators, got {a!r}")
        out.append(plain)
    return out


def _raw_input(call: dict) -> dict:
    inp = call.get("rawInput")
    if not isinstance(inp, dict):
        inp = call.get("args")
    return inp if isinstance(inp, dict) else {}


def call_paths(call: dict) -> list[str]:
    """Every path a tool call names: its ACP locations and path-like arguments."""
    out = [loc["path"] for loc in call.get("locations") or []
           if isinstance(loc, dict) and isinstance(loc.get("path"), str)]
    out += [v for k, v in _raw_input(call).items() if isinstance(v, str) and v and PATH_KEY.search(k)]
    return out


def resolve_in(p: str, root: Path) -> Path | None:
    if re.match(r"^/[A-Za-z]:[/\\]", p):     # "/C:/..." as in agy's titles
        p = p[1:]
    q = Path(p)
    if not q.is_absolute():
        q = root / q
    try:
        return q.resolve()                    # follows links: a link out of the worktree is outside
    except (OSError, RuntimeError):
        return None


class Policy:
    """What agy may do in one worktree."""

    def __init__(self, root: Path, commands: list[str]):
        self.root = root.resolve()
        self.commands = commands

    def inside(self, p: str) -> tuple[bool, str]:
        r = resolve_in(p, self.root)
        if r is None or not (r == self.root or self.root in r.parents):
            return False, f"outside the worktree: {p}"
        rel = r.relative_to(self.root).parts
        if rel and rel[0].lower() == ".git":
            return False, f"git metadata: {p}"
        if any(fnmatch.fnmatch(r.name.lower(), pat) for pat in SECRET_NAMES):
            return False, f"may hold secrets: {p}"
        return True, ""

    def decide(self, call: dict) -> tuple[bool, str]:
        kind, name = call.get("kind"), call.get("name") or ""
        if "subagent" in name.lower():
            return False, "subagents are not allowed"
        if kind == "execute":
            inp = _raw_input(call)
            cmd = inp.get("CommandLine") or inp.get("command") or call.get("title") or ""
            if not self.commands:
                return False, f"shell commands are not allowed: {cmd}"
            if not command_allowed(cmd, self.commands):
                return False, f"command not in allow_commands: {cmd}"
            # agy asks for BypassSandbox: true when a command fails in the sandbox (seen with
            # `python -m unittest`, agy 1.5.14); an allowed command still runs only inside it
            if any(v not in (False, None, "", 0, "false") for k, v in inp.items()
                   if "bypass" in k.lower() or "unsandbox" in k.lower()):
                return False, f"running a command outside the sandbox is not allowed: {cmd}"
            for p in call_paths(call):          # e.g. its working folder
                ok, why = self.inside(p)
                if not ok:
                    return False, why
            return True, ""
        if kind in FILE_KINDS:
            paths = call_paths(call)
            if not paths:
                return False, f"{name or kind} names no path to check"
            for p in paths:
                ok, why = self.inside(p)
                if not ok:
                    return False, why
            return True, ""
        return False, f"{name or kind} is not allowed"


def pick(options: list, allow: bool) -> dict:
    kinds = ("allow_once",) if allow else ("reject_once", "reject_always")
    chosen = next((o for k in kinds for o in options or [] if o.get("kind") == k), None)
    if chosen is None:
        return {"outcome": {"outcome": "cancelled"}}
    return {"outcome": {"outcome": "selected", "optionId": chosen.get("optionId")}}


# ---------------------------------------------------------------- isolation check

class _CheckTurn:
    def __init__(self, target: Path):
        self.target = target
        self.asked = False
        self.unasked: list[str] = []
        self.allowed_ids: set = set()
        self.chunks: list[str] = []

    def on_permission(self, params: dict) -> dict:
        call = params.get("toolCall") or {}
        if call.get("kind") == "read" and any(resolve_in(p, self.target.parent) == self.target
                                              for p in call_paths(call)):
            self.asked = True
        return pick(params.get("options") or [], False)

    def on_update(self, u: dict) -> None:
        kind = u.get("sessionUpdate")
        if kind == "agent_message_chunk":
            c = u.get("content") or {}
            if isinstance(c.get("text"), str):
                self.chunks.append(c["text"])
        elif kind in ("tool_call", "tool_call_update"):
            name = u.get("name") or ""
            if "subagent" in name.lower() or (u.get("status") == "completed" and u.get("kind") in WATCHED_KINDS
                                              and name not in NO_ASK_TOOLS):
                self.unasked.append(f"{name or u.get('kind')} ({u.get('title')})")


CHECK_WORD = "kestrel-7731"


def isolation_check(client: AcpClient, folder: Path, model: str) -> None:
    """agy must ask before reading a file. Under the user's own settings (say read_file(*) allowed,
    or proceed-in-sandbox) it would not, which means --cli_dir_name was ignored."""
    folder.mkdir(parents=True, exist_ok=True)
    folder = folder.resolve()   # long names: see "Paths given to agy" in the module docstring
    target = folder / "check.txt"
    target.write_text(f"{CHECK_WORD}\n", encoding="utf-8")
    turn = _CheckTurn(target)
    try:
        sid = client.request("session/new", {"cwd": str(folder), "mcpServers": []})["sessionId"]
    except (RpcError, TimeoutError, TypeError, KeyError) as e:
        raise Fail(f"isolation check: agy acp could not open a session: {e}")
    client.attach(sid, turn)
    try:
        client.request("session/set_mode", {"sessionId": sid, "modeId": "default"})
        client.request("session/set_model", {"sessionId": sid, "modelId": model})
        client.request("session/prompt", {"sessionId": sid, "prompt": [{"type": "text", "text": (
            f"Read the file {target} with your file-reading tool and reply with its first word. "
            "Do not run commands, do not use subagents.")}]}, CHECK_TIMEOUT)
    except TimeoutError:
        raise Fail("isolation check: agy did not answer in time")
    except RpcError as e:
        raise Fail(f"isolation check failed to run: {e}")
    finally:
        client.detach(sid)
        try:
            client.request("session/close", {"sessionId": sid}, 10)
        except Exception:  # noqa: BLE001 - best effort
            pass
    leaked = CHECK_WORD in "".join(turn.chunks) and not turn.asked
    if turn.unasked or leaked:
        raise Fail(f"isolation check failed: agy acted without asking ({turn.unasked or 'read the check file'}). "
                   f"`--cli_dir_name {CLI_DIR_NAME}` may no longer be honoured by this agy, so its own "
                   "settings.json applies. The task was not started.")
    if not turn.asked:
        raise Fail("isolation check inconclusive: agy did not ask to read the check file (it may not have "
                   "tried). The task was not started; try again.")


# ---------------------------------------------------------------- git

def git(args: list[str], cwd: Path, raw: bool = False):
    try:
        r = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, stdin=subprocess.DEVNULL)
    except FileNotFoundError:
        raise Fail("git not found")
    if r.returncode != 0:
        raise Fail(f"git {' '.join(args[:3])} failed: {decode(r.stderr).strip()[:800]}")
    return r.stdout if raw else decode(r.stdout).strip()


# ---------------------------------------------------------------- one task

def valid_task_id(task_id: str) -> str:
    try:
        return uuid.UUID(task_id).hex
    except (ValueError, AttributeError, TypeError):
        raise Fail(f"task_id must be one returned by gemini_delegate_start, got {task_id!r}")


class Task:
    def __init__(self, tid: str, folder: Path, repo: Path, base: str, base_sha: str, branch: str,
                 worktree: Path, task: str, model: str, commands: list[str], session_id: str | None):
        self.id, self.folder, self.repo, self.base, self.base_sha = tid, folder, repo, base, base_sha
        self.branch, self.worktree, self.task, self.model = branch, worktree, task, model
        self.commands, self.session_id = commands, session_id
        self.policy = Policy(worktree, commands)
        self.lock = threading.RLock()
        self.changed = threading.Condition(self.lock)
        self.status = "starting"
        self.error: str | None = None
        self.events: list[dict] = []
        self.rev = 0                      # bumped on every event change; status(since=) uses it
        self._tool_at: dict[str, int] = {}
        self.approved: set = set()
        self.denied: list[dict] = []
        self.warnings: list[str] = []
        self.chunks: list[str] = []
        self.response = ""
        self.stop_reason: str | None = None
        self.turns = 0
        self.nudges = 0
        self._turn_denied: list[str] = []
        self.started = time.time()
        self.created_at = now()
        self.finished: float | None = None
        self.changes: dict | None = None
        self.discarded = False
        self.client: AcpClient | None = None
        self.sid: str | None = None
        self._idle: threading.Timer | None = None
        self.deadline = self.started + timeout_seconds()

    # ------------------------------------------------ events from agy (reader thread)

    def _event(self, ev: dict) -> None:
        ev["i"], ev["t"] = len(self.events), round(time.time() - self.started, 1)
        self.events.append(ev)
        self._touch(ev)

    def _touch(self, ev: dict) -> None:
        """An event changed (a tool call's status or permission is updated in place)."""
        self.rev += 1
        ev["rev"] = self.rev

    def on_update(self, u: dict) -> None:
        kind = u.get("sessionUpdate")
        if kind == "agent_message_chunk":
            c = u.get("content") or {}
            if c.get("type") == "text" and isinstance(c.get("text"), str):
                with self.lock:
                    self.chunks.append(c["text"])
            return
        if kind not in ("tool_call", "tool_call_update"):
            return
        violation = None
        with self.lock:
            cid = u.get("toolCallId") or f"_{len(self.events)}"
            if cid in self._tool_at:
                ev = self.events[self._tool_at[cid]]
            else:
                self._tool_at[cid] = len(self.events)
                ev = {"type": "tool"}
                self._event(ev)
            for k_src, k_dst in (("name", "tool"), ("kind", "kind"), ("title", "title"), ("status", "status")):
                if u.get(k_src) is not None:
                    ev[k_dst] = u[k_src] if k_src != "title" else str(u[k_src])[:300]
            self._touch(ev)
            name = ev.get("tool") or ""
            if "subagent" in name.lower():
                violation = f"agy started a subagent ({ev.get('title')}); subagents run without asking"
            elif (ev.get("status") == "completed" and cid not in self.approved
                  and ev.get("kind") in WATCHED_KINDS and name not in NO_ASK_TOOLS):
                violation = f"agy ran {name or ev.get('kind')} ({ev.get('title')}) without asking"
        if violation:
            self.stop(violation)

    def on_permission(self, params: dict) -> dict:
        call = params.get("toolCall") or {}
        allow, why = self.policy.decide(call)
        with self.lock:
            cid = call.get("toolCallId")
            if allow and cid:
                self.approved.add(cid)
            if cid in self._tool_at:
                ev = self.events[self._tool_at[cid]]
                ev["permission"] = "allowed" if allow else f"denied: {why}"
                self._touch(ev)
            else:
                self._event({"type": "permission", "tool": call.get("name"), "kind": call.get("kind"),
                             "title": str(call.get("title"))[:300],
                             "permission": "allowed" if allow else f"denied: {why}"})
            if not allow:
                self.denied.append({"tool": call.get("name"), "kind": call.get("kind"),
                                    "title": call.get("title"), "reason": why})
                self._turn_denied.append(why)
        return pick(params.get("options") or [], allow)

    # ------------------------------------------------ running

    def busy(self) -> bool:
        """Running, or ended but finish() has not yet collected the changes and saved."""
        with self.lock:
            return self.status in RUNNING or self.finished is None

    def stop(self, why: str, status: str = "stopped") -> None:
        """End the task now: kill agy (subagents included)."""
        with self.lock:
            if self.status not in RUNNING:
                return
            self.status, self.error = status, why
            if status == "stopped":
                self.warnings.append(why)
        threading.Thread(target=self._close_client, daemon=True).start()

    def _close_client(self) -> None:
        with self.lock:
            if self._idle:
                self._idle.cancel()
                self._idle = None
            c = self.client
        if c is not None:
            c.close()

    def _prompt(self, text: str) -> bool:
        """One turn. False when the task was stopped or cancelled meanwhile."""
        with self.lock:
            self.chunks, self._turn_denied = [], []
            client, sid = self.client, self.sid
        left = self.deadline - time.time()
        if left <= 0:
            raise Fail(f"time limit reached ({timeout_seconds():.0f} s, GEMINI_BUDDY_DELEGATE_TIMEOUT)")
        try:
            result = client.request("session/prompt", {"sessionId": sid,
                                                       "prompt": [{"type": "text", "text": text}]}, left)
        except TimeoutError:
            raise Fail(f"agy did not finish within the time limit ({timeout_seconds():.0f} s, "
                       "GEMINI_BUDDY_DELEGATE_TIMEOUT); stopped")
        except (RpcError, Fail) as e:
            with self.lock:
                if self.status not in RUNNING:
                    return False
            raise Fail(f"agy acp failed: {e}")
        with self.lock:
            if self.status not in RUNNING:
                return False
            self.turns += 1
            self.response = "".join(self.chunks).strip()
            self.stop_reason = (result or {}).get("stopReason")
            if self.response:
                self._event({"type": "message", "text": self.response[:TEXT_TAIL]})
        return True

    def _work(self, text: str) -> None:
        """A turn, then nudges after turns that a rejection ended."""
        if not self._prompt(text):
            return
        while True:
            with self.lock:
                denied = list(self._turn_denied)
                if not denied or self.nudges >= MAX_NUDGES or self.stop_reason != "end_turn":
                    return
                self.nudges += 1
                self._event({"type": "note", "text": f"nudged to continue after: {denied[-1]}"})
            if not self._prompt(nudge_text(denied)):
                return

    def run(self, make_client: Callable[[Path, Path | None], AcpClient], check_dir: Path) -> None:
        try:
            write_settings()
            client = make_client(self.worktree, self.folder / "agy-acp.log")
            with self.lock:
                ended = self.status not in RUNNING
                if not ended:
                    self.client = client
            if ended:      # cancelled meanwhile: still finish, so cancel() and task.json see it
                return self.finish(self.status)
            client.start()
            with self.lock:
                ended = self.status not in RUNNING
                if not ended:
                    self.status = "checking"
            if ended:
                return self.finish(self.status)
            isolation_check(client, check_dir, self.model)
            try:
                sid = client.request("session/new", {"cwd": str(self.worktree), "mcpServers": []})["sessionId"]
            except (RpcError, TimeoutError, TypeError, KeyError) as e:
                raise Fail(f"agy acp could not open a session: {e}")
            client.attach(sid, self)
            try:   # never prompt unless the mode is "default" (agy asks) as set here
                client.request("session/set_mode", {"sessionId": sid, "modeId": "default"})
                client.request("session/set_model", {"sessionId": sid, "modelId": self.model})
            except (RpcError, TimeoutError) as e:
                raise Fail(f"agy acp failed to set up the session: {e}")
            with self.lock:
                ended = self.status not in RUNNING
                if not ended:
                    self.sid, self.status = sid, "running"
            if ended:
                return self.finish(self.status)
            self._work(first_prompt(self))
            self.finish("done")
        except Fail as e:
            self.finish("failed", str(e))
        except Exception as e:  # noqa: BLE001 - report, never leave the task "running"
            self.finish("failed", f"{type(e).__name__}: {e}")

    def reply(self, message: str) -> None:
        with self.lock:
            if self.busy():
                raise Fail("the task is still running; wait for it (gemini_delegate_status)")
            if self.status != "done" or self.client is None or not self.client.alive() or self.discarded:
                raise Fail(f"cannot continue this task (status {self.status}"
                           f"{', agy was closed after being idle' if self.status == 'done' else ''}); "
                           "start a new one")
            if self._idle:
                self._idle.cancel()
                self._idle = None
            self.status, self.error, self.nudges, self.finished = "running", None, 0, None
            self.deadline = time.time() + timeout_seconds()
            self._event({"type": "note", "text": f"reply: {message[:300]}"})
            self.save()    # a crash during this turn must read back as interrupted, not done
        threading.Thread(target=self._reply_run, args=(message,), daemon=True,
                         name=f"delegate-{self.id[:8]}").start()

    def _reply_run(self, message: str) -> None:
        try:
            self._work(message)
            self.finish("done")
        except Fail as e:
            self.finish("failed", str(e))
        except Exception as e:  # noqa: BLE001
            self.finish("failed", f"{type(e).__name__}: {e}")

    def finish(self, status: str, error: str | None = None) -> None:
        with self.lock:
            if self.status in RUNNING:
                self.status = status
                if error:
                    self.error = error
            final = self.status
        if final != "done":
            self._close_client()
        if not self.discarded:
            try:
                changes = collect_changes(self)
            except Fail as e:
                changes = {"error": str(e)}
            with self.lock:
                self.changes = changes
        with self.lock:
            if final == "done" and self.client is not None and self.status == "done":
                self._idle = threading.Timer(idle_seconds(), self._close_client)
                self._idle.daemon = True
                self._idle.start()
            self.finished = time.time()   # only now: status() and cancel() wait for this
            self.save()
            self.changed.notify_all()

    # ------------------------------------------------ views

    def record(self) -> dict:
        with self.lock:
            return {"task_id": self.id, "status": self.status, "error": self.error, "task": self.task,
                    "model": self.model, "repo": str(self.repo), "base": self.base, "base_sha": self.base_sha,
                    "branch": self.branch, "worktree": str(self.worktree), "allow_commands": self.commands,
                    "session_id": self.session_id, "created_at": self.created_at,
                    "seconds": round((self.finished or time.time()) - self.started, 1),
                    "turns": self.turns, "nudges": self.nudges, "stop_reason": self.stop_reason,
                    "response": self.response, "events": [dict(e) for e in self.events], "rev": self.rev,
                    "denied": [dict(d) for d in self.denied], "warnings": list(self.warnings),
                    "changes": dict(self.changes) if self.changes else self.changes,
                    "settled": self.finished is not None, "discarded": self.discarded}

    def save(self) -> None:
        path = self.folder / "task.json"
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.record(), ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, path)


def first_prompt(t: Task) -> str:
    if t.commands:
        cmds = ("Shell commands: only these may run, as written, optionally followed by arguments "
                "(no pipes, redirects, `&&`, `;`, `$`, `%`): " + ", ".join(f"`{c}`" for c in t.commands)
                + ". Any other command is denied. Commands run in a sandbox; asking to bypass it is "
                "denied, so if one fails there, say so in your summary instead.")
    else:
        cmds = "Shell commands: none. You have no terminal; any command is denied."
    try:
        files = git(["ls-files"], t.worktree).splitlines()
    except Fail:
        files = []
    listing = "\n".join(files[:LISTING_LINES])
    if len(files) > LISTING_LINES:
        listing += f"\n… ({len(files) - LISTING_LINES} more files)"
    return (
        f"You are doing a task in a git worktree: {t.worktree}\n"
        "Rules:\n"
        f"- Read and write files only inside {t.worktree}. Anything outside it is denied.\n"
        f"- {cmds}\n"
        "- A denied action ends your turn, so do not try one.\n"
        "- Do not use subagents (invoke_subagent): the task is stopped if you do.\n"
        "- Do not commit, push or switch branches. Leave your work as edited files; a reviewer will read the diff.\n"
        "- Search the web only if the task needs it.\n"
        "- When done, reply with a short summary: what you changed and why, what you could not check, "
        "and anything left undone.\n\n"
        f"Files in the repository (git ls-files):\n{listing}\n\n"
        f"Task:\n{t.task}"
    )


def nudge_text(denied: list[str]) -> str:
    reasons = "; ".join(dict.fromkeys(denied))
    return (f"The permission policy denied: {reasons}. That ended your turn, but the task may not be "
            "finished. Continue without that action (it will be denied again). If the task cannot be "
            "finished without it, stop and say what is left and why.")


def collect_changes(t: Task) -> dict:
    """Stage everything in the worktree and diff it against the base commit."""
    git(["add", "-A"], t.worktree)
    # plain diffs whatever the user's git config says (color.ui=always, diff.external): the patch
    # must apply with `git apply`
    diff = ["diff", "--cached", "--no-color", "--no-ext-diff"]
    names = git([*diff, "--name-status", t.base_sha], t.worktree)
    stat = git([*diff, "--stat", t.base_sha], t.worktree)
    patch = git([*diff, "--binary", t.base_sha], t.worktree, raw=True)
    path = t.folder / "changes.patch"
    path.write_bytes(patch)
    files = [{"status": line.split("\t", 1)[0], "path": line.split("\t")[-1]}
             for line in names.splitlines() if "\t" in line]
    return {"files": files, "stat": stat, "patch_file": str(path), "patch_bytes": len(patch)}


# ---------------------------------------------------------------- manager

_tasks: dict[str, Task] = {}
_tasks_lock = threading.Lock()
make_client: Callable[[Path, Path | None], AcpClient] = default_client


def tasks_dir(home: Path) -> Path:
    return home / "delegate"


def start(home: Path, repo: str, task: str, model: str, base: str | None = None,
          allow_commands: list[str] | None = None, session_id: str | None = None) -> Task:
    if not (task or "").strip():
        raise Fail("task is empty")
    commands = check_commands(allow_commands)
    src = Path(repo).expanduser().resolve()
    if not src.is_dir():
        raise Fail(f"repo not found: {repo}")
    top = Path(git(["rev-parse", "--show-toplevel"], src)).resolve()
    base = base or "HEAD"
    base_sha = git(["rev-parse", "--verify", "--end-of-options", f"{base}^{{commit}}"], top)
    tid = uuid.uuid4().hex
    folder = tasks_dir(home) / tid
    folder.mkdir(parents=True)
    wt = folder / "wt"
    branch = f"gemini-buddy/{tid[:8]}"
    git(["worktree", "add", "-q", "-b", branch, str(wt), base_sha], top)
    t = Task(tid, folder, top, base, base_sha, branch, wt.resolve(), task.strip(), model, commands, session_id)
    t.save()
    with _tasks_lock:
        _tasks[tid] = t
    threading.Thread(target=t.run, args=(make_client, tasks_dir(home) / "_check"), daemon=True,
                     name=f"delegate-{tid[:8]}").start()
    return t


def _load(home: Path, task_id: str) -> Task | dict:
    tid = valid_task_id(task_id)
    with _tasks_lock:
        t = _tasks.get(tid)
    if t is not None:
        return t
    path = tasks_dir(home) / tid / "task.json"
    if not path.exists():
        raise Fail(f"no task {task_id}")
    rec = json.loads(path.read_text(encoding="utf-8"))
    if rec.get("status") in RUNNING:
        rec["status"] = "interrupted"
        rec["error"] = "the server stopped while this task ran (its agy was closed with it)"
    rec["settled"] = True   # nothing will finish it any more
    return rec


def status(home: Path, task_id: str, since: int = 0, wait_seconds: float = 0.0) -> dict:
    t = _load(home, task_id)
    if isinstance(t, Task) and wait_seconds > 0:
        end = time.time() + min(wait_seconds, STATUS_WAIT_MAX)
        with t.changed:
            while t.busy() and (left := end - time.time()) > 0:
                t.changed.wait(min(left, 1.0))
    rec = t.record() if isinstance(t, Task) else t
    return view(rec, since)


def view(rec: dict, since: int) -> dict:
    events = rec.get("events") or []
    shown = [e for e in events if e.get("rev", 0) > since]   # new events and ones updated since
    cut = max(0, len(shown) - EVENTS_SHOWN)
    out = {k: rec.get(k) for k in ("task_id", "status", "error", "model", "branch", "worktree", "base",
                                   "base_sha", "seconds", "turns", "nudges")}
    out["events"] = shown[cut:]
    if cut:
        out["events_skipped"] = cut
    out["next_since"] = rec.get("rev") or max((e.get("rev", 0) for e in events), default=0)
    if rec.get("denied"):
        out["denied"] = rec["denied"]
    if rec.get("warnings"):
        out["warnings"] = rec["warnings"]
    if rec.get("status") in RUNNING or not rec.get("settled", True):
        return out
    out["response"] = rec.get("response") or ""
    ch = rec.get("changes")
    if ch:
        out["changes"] = dict(ch)
        pf = ch.get("patch_file")
        if pf and Path(pf).exists():
            text = decode(Path(pf).read_bytes())
            out["changes"]["patch"] = text if len(text) <= PATCH_CHARS else \
                text[:PATCH_CHARS] + f"\n…[{len(text) - PATCH_CHARS} chars cut; read patch_file]"
    if rec.get("discarded"):
        out["discarded"] = True
    return out


def reply(home: Path, task_id: str, message: str) -> dict:
    if not (message or "").strip():
        raise Fail("message is empty")
    t = _load(home, task_id)
    if not isinstance(t, Task):
        raise Fail(f"cannot continue this task (status {t.get('status')}); start a new one")
    t.reply(message.strip())
    rec = t.record()
    return view(rec, rec["rev"])


def cancel(home: Path, task_id: str, discard: bool = False) -> dict:
    t = _load(home, task_id)
    if isinstance(t, Task):
        if t.busy():
            t.stop("cancelled by the caller", status="cancelled")
            with t.changed:   # let the runner see the closed process, collect the changes and save
                end = time.time() + 15
                while t.busy() and time.time() < end:
                    t.changed.wait(0.5)
        else:
            t._close_client()
        if discard and t.busy():
            raise Fail("the task is still finishing (collecting its changes); call cancel again to discard")
        rec = t.record()
    else:
        rec = t
    if discard:
        repo, wt, branch = Path(rec["repo"]), Path(rec["worktree"]), rec["branch"]
        errors = []
        if wt.exists():
            try:
                git(["worktree", "remove", "--force", str(wt)], repo)
            except Fail as e:
                errors.append(str(e))
        try:
            git(["branch", "-D", branch], repo)
        except Fail as e:
            errors.append(str(e))
        rec["discarded"] = True
        if isinstance(t, Task):
            with t.lock:
                t.discarded = True
                t.save()
        else:
            path = tasks_dir(home) / valid_task_id(task_id) / "task.json"
            path.write_text(json.dumps(rec, ensure_ascii=False, indent=2), encoding="utf-8")
        out = view(rec, rec.get("rev") or 0)
        if errors:
            out["discard_errors"] = errors
        return out
    return view(rec, rec.get("rev") or 0)


def close_all() -> None:
    with _tasks_lock:
        tasks = list(_tasks.values())
    for t in tasks:
        with t.lock:
            if t.status in RUNNING:
                t.status, t.error = "interrupted", "the server stopped"
        t._close_client()
        try:
            with t.lock:
                t.save()
        except OSError:
            pass


atexit.register(close_all)


def reset_for_tests() -> None:
    close_all()
    with _tasks_lock:
        _tasks.clear()

