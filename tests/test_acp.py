"""The ACP backend against a fake `agy acp` (tests/fake_acp_agent.py): no agy, no network."""
import asyncio
import json
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import server  # noqa: E402
from gemini_buddy import acp  # noqa: E402

FAKE = str(Path(__file__).with_name("fake_acp_agent.py"))


class AcpBase(unittest.TestCase):
    mode = "ok"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)   # cleanups run last-first: this one after acp.reset
        self.root = Path(self.tmp.name)
        self.log = self.root / "agent.jsonl"
        env = {"GEMINI_BUDDY_HOME": str(self.root / "home"), "GEMINI_BUDDY_AGY_BACKEND": "acp",
               "FAKE_ACP_MODE": self.mode, "FAKE_ACP_LOG": str(self.log),
               "GEMINI_API_KEY": "test-key", "GOOGLE_API_KEY": "x"}
        patcher = mock.patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)
        acp.reset()
        make = lambda: acp.AcpClient(sys.executable, self.root, server.agy_env(), None, args=[FAKE])  # noqa: E731
        p2 = mock.patch.object(server, "acp_client", side_effect=lambda: acp.shared(make))
        p2.start()
        self.addCleanup(p2.stop)
        self.addCleanup(acp.reset)

    def sent(self, method):
        return [m for m in map(json.loads, self.log.read_text(encoding="utf-8").splitlines())
                if m.get("method") == method]


class AcpOk(AcpBase):
    def test_prompt_streams_answer_and_uses_plan_mode(self):
        res = server.run_agy("hello", "gemini-x", [self.root / "a", self.root / "a"])
        self.assertEqual(res["response"], "こんにちは、00000001")
        self.assertEqual(res["conversation_id"], "00000001-aaaa-bbbb-cccc-dddddddddddd")
        self.assertEqual(res["denied_actions"], [])
        self.assertEqual(res["tools"][0]["tool"], "read_file")
        self.assertEqual(self.sent("session/set_mode")[0]["params"]["modeId"], "plan")
        self.assertEqual(self.sent("session/set_model")[0]["params"]["modelId"], "gemini-x")
        self.assertEqual(self.sent("authenticate")[0]["params"]["methodId"], "oauth-personal")
        new = self.sent("session/new")[0]["params"]
        self.assertEqual(new["additionalDirectories"], [str(self.root / "a")])
        self.assertEqual(len(self.sent("session/close")), 1)
        self.assertTrue(self.sent("session/prompt")[0]["params"]["prompt"][0]["text"].startswith("Rules"))

    def test_permission_requests_are_rejected_and_reported(self):
        res = server.run_agy("PERMISSION please", "m", [])
        self.assertIn("permission=r", res["response"])
        self.assertEqual(res["denied_actions"], [{"tool": "run_command", "kind": "execute", "title": "echo hi"}])

    def test_one_process_serves_parallel_prompts(self):
        out = []
        ts = [threading.Thread(target=lambda: out.append(server.run_agy("hi", "m", [])["response"]))
              for _ in range(4)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        self.assertEqual(len(set(out)), 4)          # each got its own session's text
        self.assertEqual(len(self.sent("initialize")), 1)

    def test_child_gets_no_api_keys(self):
        c = server.acp_client()
        self.assertNotIn("GEMINI_API_KEY", c.env)
        self.assertNotIn("GOOGLE_API_KEY", c.env)
        self.assertEqual(c.tier, "Fake Pro")

    def test_consult_goes_through_acp(self):
        with mock.patch("subprocess.run") as run:
            r = asyncio.run(server.gemini_consult("Is this ok?"))
        run.assert_not_called()
        self.assertTrue(r["response"].startswith("こんにちは"))

    def test_image_generation_stays_on_cli(self):
        with mock.patch.object(server, "run_agy_cli", return_value={"response": ""}) as cli:
            server.run_agy("p", "m", [], mode="accept-edits", need_text=False)
        cli.assert_called_once()
        self.assertFalse(self.log.exists())

    def test_restarts_after_the_process_dies(self):
        server.run_agy("hi", "m", [])
        server.acp_client().proc.kill()
        server.acp_client().proc.wait(5)
        self.assertEqual(server.run_agy("hi", "m", [])["response"][:5], "こんにちは")
        self.assertEqual(len(self.sent("initialize")), 2)


class AcpNoAuth(AcpBase):
    mode = "noauth"

    def test_login_hint(self):
        with self.assertRaises(server.Fail) as cm:
            server.run_agy("hi", "m", [])
        self.assertIn("agy acp --login", str(cm.exception))


class AcpHang(AcpBase):
    mode = "hang"

    def test_timeout_cancels(self):
        with mock.patch.object(server, "AGY_TIMEOUT", 1):
            with self.assertRaises(server.Fail) as cm:
                server.run_agy("hi", "m", [])
        self.assertIn("cancelled", str(cm.exception))
        self.assertEqual(len(self.sent("session/cancel")), 1)


class AcpCrash(AcpBase):
    mode = "crash"

    def test_crash_is_a_fail_not_a_hang(self):
        with self.assertRaises(server.Fail):
            server.run_agy("hi", "m", [])


class BackendSetting(unittest.TestCase):
    def test_bad_value(self):
        with mock.patch.dict(os.environ, {"GEMINI_BUDDY_AGY_BACKEND": "nope"}):
            with self.assertRaises(server.Fail):
                server.agy_backend()


if __name__ == "__main__":
    unittest.main()
