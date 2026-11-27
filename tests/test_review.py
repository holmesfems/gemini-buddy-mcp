"""Tests for gemini_buddy/review.py that do not call agy (ported from agy-media).

Run: python -m unittest discover -s tests
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from gemini_buddy import review  # noqa: E402
from gemini_buddy.errors import Fail  # noqa: E402


def git(repo, *args):
    subprocess.run(["git", "-C", str(repo), "-c", "user.email=t@t", "-c", "user.name=t",
                    "-c", "core.autocrlf=false", *args], check=True, capture_output=True)


# Dummy secrets are put together at run time, so this file itself does not look like it
# holds secrets when review.py scans a diff that touches it.
KEY, PW, TOK, VAL = "api" + "_key", "pass" + "word", "to" + "ken", "abcdefghijkl" + "1234"


class SecretLine(unittest.TestCase):
    def test_assignment_forms(self):
        for line in [f'+{KEY} = "{VAL}"',
                     f'+  "{KEY}": "{VAL}",',
                     f"+  '{PW}': '{VAL}',",
                     f'+  {TOK}: "{VAL}"']:
            with self.subTest(line=line):
                self.assertTrue(review.SECRET_LINE.search(line))

    def test_ordinary_code_passes(self):
        for line in ['+const tokenCount = countTokens(text)', '+  "password": "",', '+api_key = os.environ["API_KEY"]']:
            with self.subTest(line=line):
                self.assertFalse(review.SECRET_LINE.search(line))

    def test_variable_references_are_not_secrets(self):
        name = "GEMINI_" + "API_KEY"
        for line in [f'+claude mcp add x -e {name}="${name}" -- run',
                     f'+  {KEY}: "${{{name}}}"',
                     f'+  {KEY}: "${{{{ secrets.{name} }}}}"',
                     f'+set {KEY}="%{name}%"',
                     f'+$env:{name}="$env:{name}_FROM_USER"']:
            with self.subTest(line=line):
                self.assertFalse(review.SECRET_LINE.search(line))
        # values that only start with $ or % are not references and still count
        for line in [f'+{KEY} = "{VAL}$x"',
                     f'+{PW} = "$2b$12$eImiTXuWVxfM37uY4JANjQ"',   # bcrypt hash
                     f'+{PW} = "$uperSecretPass123!"',
                     f'+{TOK} = "%2FrealSecretToken123"',          # URL-encoded
                     f'+{KEY} = "${{{VAL}"']:                      # unclosed brace
            with self.subTest(line=line):
                self.assertTrue(review.SECRET_LINE.search(line))

    def test_removed_and_context_lines_count(self):
        key = "AIza" + "A" * 35
        diff = (f"diff --git a/a.ts b/a.ts\n--- a/a.ts\n+++ b/a.ts\n@@ -1,3 +1,3 @@\n"
                f" const keep = \"{key}\"\n-const old = \"{key}\"\n+const x = 1\n")
        hits = review.secret_like_lines(diff)
        self.assertEqual(len(hits), 2)
        self.assertEqual({h["file"] for h in hits}, {"a.ts"})

    def test_header_lines_are_not_content(self):
        diff = "diff --git a/x b/x\n--- a/x\n+++ b/x\n@@ -1 +1 @@\n-a\n+b\n"
        self.assertEqual(review.secret_like_lines(diff), [])


class ParseAnswer(unittest.TestCase):
    def test_pretty_json_is_not_cut_to_one_finding(self):
        text = '{\n  "findings": [\n    {"file": "a.py", "line": 10}\n  ]\n}'
        self.assertEqual(review.parse_answer(text), {"findings": [{"file": "a.py", "line": 10}]})

    def test_fenced_multiline_json(self):
        text = '結果です\n```json\n{\n  "findings": [],\n  "notes": "x"\n}\n```\n'
        self.assertEqual(review.parse_answer(text), {"findings": [], "notes": "x"})

    def test_json_after_prose(self):
        text = 'I looked at {the diff}. {"findings": [{"file": "b.ts", "line": 3}]}'
        self.assertEqual(review.parse_answer(text)["findings"][0]["file"], "b.ts")

    def test_none_and_garbage(self):
        self.assertIsNone(review.parse_answer(None))
        self.assertIsNone(review.parse_answer("no json here"))
        self.assertIsNone(review.parse_json_tail(None))


class NullsFromAgy(unittest.TestCase):
    def run_with(self, agy_json):
        done = subprocess.CompletedProcess([], 0, stdout=json.dumps(agy_json).encode(), stderr=b"")
        with mock.patch.object(review.subprocess, "run", return_value=done):
            return review.run_model("m", "p", Path("."), Path("."), Path("s.json"), "agy", 10)

    def test_null_response(self):
        r = self.run_with({"status": "ERROR", "response": None})
        self.assertEqual(r["findings"], [])

    def test_null_findings(self):
        r = self.run_with({"status": "SUCCESS", "response": "", "structured_output": {"findings": None, "notes": None}})
        self.assertEqual(r["findings"], [])
        self.assertEqual(r["notes"], "")

    def test_bad_finding_items_are_dropped(self):
        r = self.run_with({"status": "SUCCESS", "structured_output": {"findings": [
            None, {"file": "a.py", "line": "12", "confidence": None}, {"line": 3}]}})
        self.assertEqual(r["findings"], [{"file": "a.py", "line": 12, "confidence": 0.0}])

    def test_one_model_crashing_keeps_the_others(self):
        with mock.patch.object(review, "run_model", side_effect=RuntimeError("boom")):
            r = review.run_model_safe("m", "p")
        self.assertEqual(r["status"], "ERROR")
        self.assertEqual(r["findings"], [])
        self.assertEqual(review.merge([r, {"model": "n", "findings": [{"file": "a", "line": 1}]}], 3)[0]["models"], ["n"])


class BuildDiff(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name)
        git(self.repo, "init", "-q", "-b", "main")
        (self.repo / "a.txt").write_text("a\n")
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-qm", "base")

    def tearDown(self):
        self.tmp.cleanup()

    def args(self, **kw):
        return Namespace(**({"commit": None, "base": None, "staged": False} | kw))

    def test_merge_commit_has_a_diff(self):
        git(self.repo, "switch", "-qc", "f")
        (self.repo / "a.txt").write_text("a\nb\n")
        git(self.repo, "commit", "-qam", "f")
        git(self.repo, "switch", "-q", "main")
        (self.repo / "c.txt").write_text("c\n")
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-qm", "m2")
        git(self.repo, "merge", "-q", "--no-edit", "f")
        diff, _ = review.build_diff(self.repo, self.args(commit="HEAD"))
        self.assertIn("+b", diff)
        self.assertNotIn("c.txt", diff)

    def test_secret_files_left_out_in_any_case(self):
        for name in (".ENV", "Credentials.json", "server.KEY", "sub/.env.local"):
            p = self.repo / name
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text("X=1\n")
        (self.repo / "b.txt").write_text("b\n")
        git(self.repo, "add", ".")
        diff, _ = review.build_diff(self.repo, self.args(staged=True))
        self.assertIn("b.txt", diff)
        for name in (".ENV", "Credentials.json", "server.KEY", ".env.local"):
            self.assertNotIn(name, diff)


class BuildFiles(BuildDiff):
    def test_whole_files_as_added_lines(self):
        (self.repo / "calc").mkdir()
        (self.repo / "calc" / "risei.py").write_text("x = 1\ny = x / 2\n")   # untracked, not ignored
        (self.repo / "calc" / ".env").write_text("X=1\n")
        (self.repo / "calc" / "img.bin").write_bytes(b"\x00\x01")
        (self.repo / ".gitignore").write_text("*.log\n")
        (self.repo / "calc" / "out.log").write_text("log\n")
        diff, skipped = review.build_files(self.repo, self.repo, ["calc"])
        self.assertIn("+++ b/calc/risei.py\n@@ -0,0 +1,2 @@\n+x = 1\n+y = x / 2\n", diff)
        for name in (".env", "out.log", "img.bin"):
            self.assertNotIn(name, diff)
        self.assertEqual(skipped, ["calc/img.bin"])

    def test_relative_to_repo_folder_and_absolute(self):
        (self.repo / "sub").mkdir()
        (self.repo / "sub" / "b.py").write_text("b\n")
        diff, _ = review.build_files(self.repo, self.repo / "sub", ["b.py", str(self.repo / "a.txt")])
        self.assertIn("+++ b/sub/b.py", diff)
        self.assertIn("+++ b/a.txt", diff)

    def test_outside_the_repository_is_refused(self):
        with tempfile.TemporaryDirectory() as other:
            with self.assertRaises(Fail):
                review.pathspecs(self.repo, self.repo, [other])


class Review(BuildDiff):
    def test_paths_and_another_target_conflict(self):
        with self.assertRaises(Fail):
            review.review(str(self.repo), staged=True, paths=["a.txt"], out=self.repo / "_w")

    def test_paths_with_nothing_to_send_is_an_error(self):
        (self.repo / ".env").write_text("X=1\n")
        with self.assertRaises(Fail):
            review.review(str(self.repo), paths=[".env"], out=Path(self.tmp.name) / "w")

    def test_paths_reviews_files_with_no_change(self):
        (self.repo / "a.txt").write_text(f'{KEY} = "{VAL}"\n')
        git(self.repo, "commit", "-qam", "secret")   # clean tree: the diff modes have nothing
        with mock.patch.object(review, "run_model") as run:
            r = review.review(str(self.repo), paths=["a.txt"], out=Path(self.tmp.name) / "w")
        self.assertEqual(r["status"], "STOPPED")      # the secret check covers whole files too
        run.assert_not_called()
        (self.repo / "a.txt").write_text("total = a / b\n")
        with mock.patch.object(review, "run_model_safe",
                               return_value={"model": "m", "status": "SUCCESS", "findings": []}):
            r = review.review(str(self.repo), paths=["a.txt"], out=Path(self.tmp.name) / "w3")
        self.assertEqual(r["status"], "SUCCESS")
        ask = (Path(self.tmp.name) / "w3" / "ask.txt").read_text(encoding="utf-8")
        self.assertIn("review existing code", ask)
        self.assertIn("+total = a / b", (Path(self.tmp.name) / "w3" / "diff.patch").read_text(encoding="utf-8"))

    def test_empty_diff_is_an_error(self):
        with self.assertRaises(Fail):
            review.review(str(self.repo), out=self.repo / "_w")

    def test_secret_like_line_stops_before_sending(self):
        (self.repo / "a.txt").write_text(f'{KEY} = "{VAL}"\n')
        with mock.patch.object(review, "run_model") as run:
            r = review.review(str(self.repo), out=Path(self.tmp.name) / "w")
        self.assertEqual(r["status"], "STOPPED")
        run.assert_not_called()

    def test_models_are_merged_and_api_keys_removed(self):
        (self.repo / "a.txt").write_text("a\nchanged\n")
        answers = {"m1": [{"file": "a.txt", "line": 2, "severity": "high", "confidence": 0.9}],
                   "m2": [{"file": "a.txt", "line": 3, "severity": "low", "confidence": 0.5}]}
        envs, cmds = [], []
        real_run = subprocess.run

        def fake_run(cmd, **kw):
            if cmd[0] == "git":
                return real_run(cmd, **kw)
            envs.append(kw["env"])
            cmds.append(cmd)
            model = cmd[cmd.index("--model") + 1]
            out = {"status": "SUCCESS", "structured_output": {"findings": answers[model]}}
            return subprocess.CompletedProcess(cmd, 0, json.dumps(out).encode(), b"")

        with mock.patch.dict(os.environ, {"GEMINI_API_KEY": "k"}), \
                mock.patch.object(review.subprocess, "run", side_effect=fake_run):
            r = review.review(str(self.repo), models=["m1", "m2"], intent="change a",
                              out=Path(self.tmp.name) / "w2")
        self.assertEqual(r["status"], "SUCCESS")
        self.assertEqual(len(r["findings"]), 1)
        self.assertEqual(r["findings"][0]["agreed"], 2)
        self.assertEqual(r["findings"][0]["severity"], "high")
        self.assertTrue(all("GEMINI_API_KEY" not in e for e in envs))
        for cmd in cmds:  # commands and writes denied over the user's allow list
            deny = json.loads(cmd[cmd.index("--settings") + 1])["permissions"]["deny"]
            self.assertIn("command(*)", deny)
        self.assertIn("change a", (Path(self.tmp.name) / "w2" / "ask.txt").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
