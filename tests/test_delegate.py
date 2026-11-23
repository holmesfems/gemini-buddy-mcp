"""gemini_delegate_* against the fake `agy acp` (tests/fake_acp_agent.py): no agy, no network."""
import asyncio
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import server  # noqa: E402
from gemini_buddy import acp, delegate  # noqa: E402

FAKE = str(Path(__file__).with_name("fake_acp_agent.py"))


class PolicyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "wt"
        self.root.mkdir()
        self.p = delegate.Policy(self.root, ["python -m unittest"])

    def call(self, kind, path=None, name="x", **raw):
        c = {"kind": kind, "name": name, "rawInput": raw}
        if path is not None:
            c["locations"] = [{"path": str(path)}]
        return c

    def test_files_inside_only(self):
        self.assertTrue(self.p.decide(self.call("edit", self.root / "a.py"))[0])
        self.assertTrue(self.p.decide(self.call("read", None, Path="sub/b.py"))[0])   # relative to the worktree
        self.assertFalse(self.p.decide(self.call("edit", self.root / ".." / "x.txt"))[0])
        self.assertFalse(self.p.decide(self.call("read", self.root / ".git" / "config"))[0])
        self.assertFalse(self.p.decide(self.call("read", self.root / ".ENV"))[0])
        self.assertFalse(self.p.decide(self.call("read", self.root / "conf" / "server.key"))[0])
        self.assertFalse(self.p.decide(self.call("edit"))[0])                        # no path to check
        # the location is inside but the argument points out: denied
        self.assertFalse(self.p.decide(self.call("edit", self.root / "a.py", Path=str(self.root.parent / "b")))[0])
        self.assertTrue(self.p.decide(self.call("read", "/" + (self.root / "a.py").as_posix()))[0]
                        if os.name == "nt" else True)

    def test_commands(self):
        run = lambda cmd, **kw: self.p.decide(self.call("execute", None, CommandLine=cmd, **kw))[0]  # noqa: E731
        self.assertTrue(run("python -m unittest"))
        self.assertTrue(run("python  -m unittest discover -s tests"))
        self.assertFalse(run("python -m unittestx"))
        self.assertFalse(run("python -m unittest && del a.py"))
        self.assertFalse(run("python -m unittest > out.txt"))
        self.assertFalse(run("python -m unittest $(evil)"))
        self.assertFalse(run("python -m unittest\ngit push"))       # a newline is a second command
        self.assertFalse(run("python -m unittest\r\ngit push"))
        self.assertFalse(run("python -m unittest git push"))
        self.assertTrue(run("python -m unittest\t-v"))
        self.assertFalse(run("git status"))
        self.assertFalse(run("python -m unittest", Cwd=str(self.root.parent)))
        self.assertTrue(run("python -m unittest", BypassSandbox=False))
        self.assertFalse(run("python -m unittest", BypassSandbox=True))   # agy's retry outside the sandbox
        self.assertFalse(delegate.Policy(self.root, []).decide(self.call("execute", None, CommandLine="ls"))[0])

    def test_other_tools_and_subagents(self):
        self.assertFalse(self.p.decide(self.call("other", name="invoke_subagent"))[0])
        self.assertFalse(self.p.decide(self.call("fetch", name="read_url"))[0])

    def test_allow_commands_must_be_plain(self):
        with self.assertRaises(delegate.Fail):
            delegate.check_commands(["npm test; rm -rf /"])
        with self.assertRaises(delegate.Fail):
            delegate.check_commands(["npm test\ngit push"])
        self.assertEqual(delegate.check_commands([" npm   test "]), ["npm test"])

    def test_agy_reads_our_settings(self):
        self.assertEqual(delegate.agy_args(), ["acp", "--cli_dir_name", "gemini-buddy-delegate"])
        with mock.patch.dict(os.environ, {"GEMINI_BUDDY_AGY_APP_DIR": self.tmp.name}):
            path = delegate.write_settings()
        self.assertEqual(path, Path(self.tmp.name) / "gemini-buddy-delegate" / "settings.json")
        s = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(s["toolPermission"], "strict")
        self.assertEqual(s["permissions"], {"allow": [], "deny": [], "ask": []})
        path.write_text("{}", encoding="utf-8")            # an edit does not survive the next start
        with mock.patch.dict(os.environ, {"GEMINI_BUDDY_AGY_APP_DIR": self.tmp.name}):
            mtime = delegate.write_settings().stat().st_mtime_ns
            self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["toolPermission"], "strict")
            self.assertEqual(delegate.write_settings().stat().st_mtime_ns, mtime)   # unchanged: not rewritten
        self.assertEqual([p.name for p in path.parent.iterdir()], ["settings.json"])


class DelegateBase(unittest.TestCase):
    mode = "ok"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.log = self.root / "agent.jsonl"
        env = {"GEMINI_BUDDY_HOME": str(self.root / "home"), "GEMINI_BUDDY_AGY_APP_DIR": str(self.root / "agyapp"),
               "FAKE_ACP_MODE": self.mode, "FAKE_ACP_LOG": str(self.log),
               "GEMINI_API_KEY": "test-key", "GOOGLE_API_KEY": "x"}
        patcher = mock.patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)
        make = lambda cwd, log: acp.AcpClient(sys.executable, cwd, server.agy_env(), None, args=[FAKE])  # noqa: E731
        p2 = mock.patch.object(delegate, "make_client", make)
        p2.start()
        self.addCleanup(p2.stop)
        self.addCleanup(delegate.reset_for_tests)
        self.repo = self.make_repo()

    def make_repo(self):
        repo = self.root / "repo"
        repo.mkdir()
        self.git(repo, "init", "-q")
        (repo / "a.py").write_text("def add(a, b):\n    return a - b\n")
        self.git(repo, "add", ".")
        self.git(repo, "commit", "-qm", "init")
        return repo

    def git(self, repo, *a):
        return subprocess.run(["git", "-C", str(repo), "-c", "user.email=t@t", "-c", "user.name=t", *a],
                              check=True, capture_output=True, text=True).stdout

    def start(self, task, **kw):
        return asyncio.run(server.gemini_delegate_start(task, str(self.repo), **kw))

    def wait(self, task_id, since=0):
        r = asyncio.run(server.gemini_delegate_status(task_id, wait_seconds=20, since=since))
        self.assertNotIn(r["status"], delegate.RUNNING, r)
        return r

    def sent(self, method):
        if not self.log.exists():
            return []
        return [m for m in map(json.loads, self.log.read_text(encoding="utf-8").splitlines())
                if m.get("method") == method]


class DelegateRuns(DelegateBase):
    def test_edit_in_worktree_comes_back_as_a_patch(self):
        session = server.open_session(None, "earlier work")   # sessions record the task
        server.save_session(session)
        s = session.session_id
        started = self.start("EDIT_INSIDE fix add", session_id=s)
        r = self.wait(started["task_id"])
        self.assertEqual(r["status"], "done", r)
        self.assertEqual(r["response"], "I fixed a.py.")
        self.assertEqual(r["changes"]["files"], [{"status": "M", "path": "a.py"}])
        self.assertIn("+    return a + b", r["changes"]["patch"])
        self.assertTrue(Path(r["changes"]["patch_file"]).exists())
        self.assertIn("return a - b", (self.repo / "a.py").read_text())        # nothing merged
        self.assertTrue(any(e.get("permission") == "allowed" for e in r["events"]))
        self.assertEqual({m["params"]["modeId"] for m in self.sent("session/set_mode")}, {"default"})
        self.assertEqual(self.sent("session/new")[0]["params"]["cwd"],     # the check runs first, in a
                         str((self.root / "home" / "delegate" / "_check").resolve()))   # long-named path
        self.assertEqual(Path(self.sent("session/new")[1]["params"]["cwd"]), Path(started["worktree"]))
        settings = self.root / "agyapp" / "gemini-buddy-delegate" / "settings.json"
        self.assertEqual(json.loads(settings.read_text())["toolPermission"], "strict")
        self.assertIn(started["branch"], self.git(self.repo, "branch"))
        saved = server.open_session(s, "")
        self.assertEqual(saved.artifacts[-1].kind, "delegate")

    @unittest.skipUnless(os.name == "nt", "8.3 short names are a Windows thing")
    def test_short_named_home_reaches_agy_long_named(self):
        import ctypes
        home = self.root / "long-named-home"
        home.mkdir()
        buf = ctypes.create_unicode_buffer(1024)
        ctypes.windll.kernel32.GetShortPathNameW(str(home), buf, len(buf))
        if not buf.value or Path(buf.value).name == home.name:
            self.skipTest("this volume makes no 8.3 short names")
        with mock.patch.dict(os.environ, {"GEMINI_BUDDY_HOME": buf.value}):
            r = self.wait(self.start("EDIT_INSIDE fix add")["task_id"])
        self.assertEqual(r["status"], "done", r)
        cwds = [m["params"]["cwd"] for m in self.sent("session/new")]
        self.assertEqual(cwds[0], str((home / "delegate" / "_check").resolve()))
        self.assertFalse(any("~" in c for c in cwds), cwds)

    def test_outside_write_is_denied_then_nudged(self):
        r = self.wait(self.start("EDIT_OUTSIDE EDIT_INSIDE")["task_id"])
        self.assertEqual(r["status"], "done")
        self.assertFalse((self.root / "home" / "delegate" / r["task_id"] / "outside.txt").exists())
        self.assertIn("outside the worktree", r["denied"][0]["reason"])
        self.assertEqual(r["nudges"], 1)
        self.assertEqual(r["response"], "continued without it")
        self.assertEqual(r["changes"]["files"], [])
        nudge = self.sent("session/prompt")[-1]["params"]["prompt"][0]["text"]
        self.assertTrue(nudge.startswith("The permission policy denied: outside the worktree"))

    def test_git_metadata_is_denied(self):
        r = self.wait(self.start("READ_GIT")["task_id"])
        self.assertIn("git metadata", r["denied"][0]["reason"])

    def test_commands_denied_unless_allowed(self):
        r = self.wait(self.start("go\nRUN: python -m unittest discover")["task_id"])
        self.assertIn("shell commands are not allowed", r["denied"][0]["reason"])
        self.assertEqual(r["changes"]["files"], [])
        r = self.wait(self.start("go\nRUN: python -m unittest discover",
                                 allow_commands=["python -m unittest"])["task_id"])
        self.assertNotIn("denied", r)
        self.assertEqual(r["changes"]["files"], [{"status": "A", "path": "ran.txt"}])
        r = self.wait(self.start("go\nRUN: python -m unittest & echo x",
                                 allow_commands=["python -m unittest"])["task_id"])
        self.assertIn("not in allow_commands", r["denied"][0]["reason"])

    def test_subagent_stops_the_task(self):
        r = self.wait(self.start("SUBAGENT please")["task_id"])
        self.assertEqual(r["status"], "stopped")
        self.assertIn("subagent", r["warnings"][0])

    def test_action_without_asking_stops_the_task(self):
        r = self.wait(self.start("SNEAKY")["task_id"])
        self.assertEqual(r["status"], "stopped")
        self.assertIn("without asking", r["error"])

    def test_cancel_and_discard(self):
        t = self.start("HANG")
        asyncio.run(server.gemini_delegate_status(t["task_id"], wait_seconds=1))
        r = asyncio.run(server.gemini_delegate_cancel(t["task_id"]))
        self.assertEqual(r["status"], "cancelled")
        self.assertTrue(Path(t["worktree"]).exists())
        r = asyncio.run(server.gemini_delegate_cancel(t["task_id"], discard=True))
        self.assertTrue(r["discarded"])
        self.assertFalse(Path(t["worktree"]).exists())
        self.assertNotIn(t["branch"], self.git(self.repo, "branch"))

    def test_cancel_right_after_start_settles_quickly(self):
        import time
        gate = __import__("threading").Event()
        real = delegate.write_settings

        def slow_settings():
            gate.wait(5)
            return real()

        with mock.patch.object(delegate, "write_settings", slow_settings):
            t = self.start("EDIT_INSIDE")
            t0 = time.time()
            threading = __import__("threading")
            threading.Timer(0.3, gate.set).start()
            r = asyncio.run(server.gemini_delegate_cancel(t["task_id"]))
        self.assertLess(time.time() - t0, 10)
        self.assertEqual(r["status"], "cancelled")
        saved = json.loads((self.root / "home" / "delegate" / t["task_id"] / "task.json").read_text(encoding="utf-8"))
        self.assertEqual(saved["status"], "cancelled")
        self.assertEqual(self.sent("session/prompt"), [])

    def test_patch_is_plain_whatever_the_git_config(self):
        self.git(self.repo, "config", "color.ui", "always")
        self.git(self.repo, "config", "diff.external", "definitely-not-a-diff-tool")
        r = self.wait(self.start("EDIT_INSIDE")["task_id"])
        self.assertNotIn("\x1b[", r["changes"]["patch"])
        self.assertNotIn("error", r["changes"])
        self.assertIn("+    return a + b", r["changes"]["patch"])

    def test_since_shows_events_updated_in_place(self):
        t = delegate.Task("0" * 32, self.root, self.repo, "HEAD", "x", "b", self.repo, "t", "m", [], None)
        t.on_update({"sessionUpdate": "tool_call", "toolCallId": "c1", "name": "read_file", "kind": "read",
                     "status": "in_progress"})
        first = delegate.view(t.record(), 0)
        t.on_permission({"toolCall": {"toolCallId": "c1", "kind": "read", "name": "read_file",
                                      "locations": [{"path": str(self.repo / "a.py")}]},
                         "options": [{"optionId": "a", "kind": "allow_once"}]})
        later = delegate.view(t.record(), first["next_since"])
        self.assertEqual([(e["i"], e["permission"]) for e in later["events"]], [(0, "allowed")])
        rec = t.record()                                   # a copy: later updates do not change it
        t.on_update({"sessionUpdate": "tool_call_update", "toolCallId": "c1", "status": "completed"})
        self.assertNotIn("completed", json.dumps(rec))

    def test_reply_continues_the_session(self):
        t = self.start("look around")
        r = self.wait(t["task_id"])
        self.assertEqual(r["changes"]["files"], [])
        with self.assertRaises(server.Fail):
            asyncio.run(server.gemini_delegate_reply(t["task_id"], ""))
        asyncio.run(server.gemini_delegate_reply(t["task_id"], "REPLY HANG"))
        saved = self.root / "home" / "delegate" / t["task_id"] / "task.json"
        self.assertEqual(json.loads(saved.read_text(encoding="utf-8"))["status"], "running")   # crash => interrupted
        asyncio.run(server.gemini_delegate_cancel(t["task_id"]))
        t = self.start("look around")
        r = self.wait(t["task_id"])
        asyncio.run(server.gemini_delegate_reply(t["task_id"], "REPLY EDIT_INSIDE"))
        r = self.wait(t["task_id"], since=r["next_since"])
        self.assertEqual(r["status"], "done")
        self.assertEqual(r["changes"]["files"], [{"status": "M", "path": "a.py"}])
        self.assertEqual(r["turns"], 2)
        self.assertEqual(len(self.sent("initialize")), 2)   # one agy per task; the reply reused it

    def test_status_after_a_restart_reads_the_saved_task(self):
        t = self.start("look around")
        self.wait(t["task_id"])
        delegate.reset_for_tests()
        r = asyncio.run(server.gemini_delegate_status(t["task_id"]))
        self.assertEqual(r["status"], "done")
        path = self.root / "home" / "delegate" / t["task_id"] / "task.json"
        rec = json.loads(path.read_text(encoding="utf-8"))
        rec["status"] = "running"
        path.write_text(json.dumps(rec), encoding="utf-8")
        self.assertEqual(asyncio.run(server.gemini_delegate_status(t["task_id"]))["status"], "interrupted")
        with self.assertRaises(server.Fail):
            asyncio.run(server.gemini_delegate_reply(t["task_id"], "more"))

    def test_bad_inputs(self):
        with self.assertRaises(server.Fail):
            asyncio.run(server.gemini_delegate_status("../../etc"))
        with self.assertRaises(server.Fail):
            asyncio.run(server.gemini_delegate_start("x", str(self.root / "nope")))
        with self.assertRaises(server.Fail):
            self.start("x", base="--output=evil")
        with self.assertRaises(server.Fail):
            self.start("x", allow_commands=["a | b"])


class DelegateIsolationBroken(DelegateBase):
    mode = "nocheck"

    def test_task_does_not_start(self):
        r = self.wait(self.start("EDIT_INSIDE")["task_id"])
        self.assertEqual(r["status"], "failed")
        self.assertIn("isolation check failed", r["error"])
        prompts = [m["params"]["prompt"][0]["text"] for m in self.sent("session/prompt")]
        self.assertFalse([p for p in prompts if "git worktree" in p])


if __name__ == "__main__":
    unittest.main()
