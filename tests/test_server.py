"""Tests that need neither agy nor an API key: sessions, the agy command and env, file writing."""
import asyncio
import json
import os
import subprocess
import sys
import tempfile
import unittest
import uuid
import wave
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import server  # noqa: E402


def fake_agy(response="looks fine", conv="c-1"):
    out = json.dumps({"conversation_id": conv, "status": "SUCCESS", "response": response,
                      "duration_seconds": 1.5}).encode()
    return subprocess.CompletedProcess([], 0, b"Warning: something\n" + out, b"")


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        patcher = mock.patch.dict(os.environ, {"GEMINI_BUDDY_HOME": str(self.root / "home"),
                                               "GEMINI_API_KEY": "test-key", "GOOGLE_API_KEY": "x"})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.tmp.cleanup)


class SessionTests(Base):
    def turn(self, text):
        return server.Turn(tool="gemini_consult", at=server.now(), prompt=text)

    def test_stale_copy_does_not_drop_what_others_saved(self):
        s = server.open_session(None, "first")
        server.save_session(s)
        stale = server.open_session(s.session_id, "")           # call A loads, then waits on a model
        other = server.open_session(s.session_id, "")           # call B loads and saves first
        server.update_session(other, lambda x: x.turns.append(self.turn("B")))
        server.update_session(stale, lambda x: x.turns.append(self.turn("A")))
        saved = server.open_session(s.session_id, "")
        self.assertEqual([t.prompt for t in saved.turns], ["B", "A"])

    def test_parallel_updates_all_land(self):
        import threading
        s = server.open_session(None, "first")
        server.save_session(s)
        threads = [threading.Thread(target=server.update_session,
                                    args=(s, lambda x, i=i: x.turns.append(self.turn(str(i)))))
                   for i in range(12)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        saved = server.open_session(s.session_id, "")
        self.assertEqual(sorted(t.prompt for t in saved.turns), sorted(str(i) for i in range(12)))

    def test_leftover_lock_file_does_not_block(self):
        """A crashed process leaves the file but not the OS lock."""
        s = server.open_session(None, "first")
        lock = server.session_path(s.session_id).with_suffix(".lock")
        lock.parent.mkdir(parents=True, exist_ok=True)
        lock.write_text("left over")
        with mock.patch.object(server, "LOCK_WAIT", 0.5):
            server.update_session(s, lambda x: x.turns.append(self.turn("x")))
        self.assertEqual(len(server.open_session(s.session_id, "").turns), 1)

    def test_busy_lock_waits_then_fails_with_a_message(self):
        import threading
        s = server.open_session(None, "first")
        held, release = threading.Event(), threading.Event()

        def holder():
            with server.session_lock(s.session_id):
                held.set()
                release.wait(5)

        t = threading.Thread(target=holder)
        t.start()
        held.wait(5)
        try:
            with mock.patch.object(server, "LOCK_WAIT", 0.3), self.assertRaises(server.Fail) as e:
                server.update_session(s, lambda x: None)
            self.assertIn("busy", str(e.exception))
        finally:
            release.set()
            t.join()
        server.update_session(s, lambda x: x.turns.append(self.turn("after")))  # free again
        self.assertEqual(len(server.open_session(s.session_id, "").turns), 1)

    def test_work_dirs_never_collide(self):
        dirs = {server.work_dir("frames", None, None) for _ in range(20)}
        self.assertEqual(len(dirs), 20)

    def test_rejects_non_uuid_ids(self):
        for bad in ("../../etc/passwd", "abc", ""):
            with self.assertRaises(server.Fail):
                server.session_path(bad) if bad else server.valid_session_id(None)

    def test_unknown_session_is_an_error(self):
        with self.assertRaises(server.Fail):
            server.open_session(str(uuid.uuid4()), "hi")

    def test_title_is_first_prompt_shortened(self):
        s = server.open_session(None, "  review   this\nfunction " + "x" * 100)
        self.assertTrue(s.title.startswith("review this function"))
        self.assertLessEqual(len(s.title), 60)

    def test_round_trip_and_context(self):
        s = server.open_session(None, "first")
        s.turns.append(server.Turn(tool="gemini_consult", at=server.now(), prompt="Q1", response="A1"))
        s.media.append(server.MediaNote(file="v.mp4", prompt="what", summary="a cat", at=server.now()))
        server.save_session(s)
        again = server.open_session(s.session_id, "ignored")
        self.assertEqual(again.title, "first")
        ctx = server.session_context(again)
        for piece in ("Q1", "A1", "v.mp4", "a cat", "## New request"):
            self.assertIn(piece, ctx)
        self.assertEqual(server.session_context(server.open_session(None, "new")), "")


class AgyTests(Base):
    def test_api_keys_are_removed_from_agy_env(self):
        env = server.agy_env()
        self.assertNotIn("GEMINI_API_KEY", env)
        self.assertNotIn("GOOGLE_API_KEY", env)
        self.assertIn("GEMINI_BUDDY_HOME", env)

    def test_command_never_skips_permissions(self):
        cmd = server.agy_command("p", "m", [Path("a"), Path("a"), Path("b")])
        self.assertNotIn("--dangerously-skip-permissions", cmd)
        self.assertEqual(cmd.count("--add-dir"), 2)
        self.assertIn("json", cmd)

    def test_agy_runs_in_an_empty_folder_not_the_servers(self):
        with mock.patch("subprocess.run", return_value=fake_agy("ok")) as run:
            server.run_agy("p", "m", [])
        cwd = Path(run.call_args.kwargs["cwd"])
        self.assertEqual(cwd, self.root / "home" / "agy-cwd")
        self.assertNotEqual(cwd.resolve(), Path.cwd().resolve())

    def test_unknown_session_fails_before_a_review(self):
        with mock.patch.object(server.review_mod, "review") as review:
            with self.assertRaises(server.Fail):
                asyncio.run(server.gemini_review(str(self.root), session_id=str(uuid.uuid4())))
        review.assert_not_called()

    def test_unknown_session_fails_before_media_work(self):
        with mock.patch.object(server.media, "frames") as frames:
            with self.assertRaises(server.Fail):
                asyncio.run(server.media_frames("x.mp4", session_id=str(uuid.uuid4())))
        frames.assert_not_called()
        self.assertFalse((self.root / "home" / "work").exists())

    def test_parse_json_after_warning(self):
        self.assertEqual(server.parse_json_tail('warn\n{"a": 1}')["a"], 1)
        self.assertIsNone(server.parse_json_tail("no json"))

    def test_consult_continues_a_session(self):
        src = self.root / "code.py"
        src.write_text("x = 1\n")
        with mock.patch("subprocess.run", return_value=fake_agy("first answer")) as run:
            r1 = asyncio.run(server.gemini_consult("Is this ok?", [str(src)]))
        self.assertEqual(r1["response"], "first answer")
        self.assertNotIn("GEMINI_API_KEY", run.call_args.kwargs["env"])
        self.assertIn(str(src.resolve()), run.call_args.args[0][2])

        with mock.patch("subprocess.run", return_value=fake_agy("second")) as run:
            r2 = asyncio.run(server.gemini_consult("And now?", session_id=r1["session_id"]))
        prompt = run.call_args.args[0][2]
        self.assertIn("first answer", prompt)
        self.assertIn("Is this ok?", prompt)
        self.assertEqual(r2["session_id"], r1["session_id"])

        listed = server.gemini_list_sessions()
        self.assertEqual(listed["count"], 1)
        self.assertEqual(listed["sessions"][0]["turns"], 2)
        self.assertIn(str(src.resolve()), listed["sessions"][0]["files"])

    def test_media_summary_is_saved(self):
        clip = self.root / "clip.mp4"
        clip.write_bytes(b"\0")
        with mock.patch("subprocess.run", return_value=fake_agy("A cat jumps at 0:03.")):
            r = asyncio.run(server.gemini_analyze_media(str(clip), "What happens?"))
        s = server.open_session(r["session_id"], "")
        self.assertEqual(s.media[0].summary, "A cat jumps at 0:03.")

    def test_several_frames_in_one_question(self):
        frames = []
        for n in (1, 2):
            f = self.root / f"f_00{n}.png"
            f.write_bytes(b"\0")
            frames.append(f)
        with mock.patch("subprocess.run", return_value=fake_agy("{}")) as run:
            r = asyncio.run(server.gemini_analyze_media(str(frames[0]), "where?", more_files=[str(frames[1])]))
        prompt = run.call_args.args[0][2]
        self.assertIn(str(frames[1].resolve()), prompt)
        files = server.gemini_list_sessions()["sessions"][0]["files"]
        self.assertEqual(sorted(files), sorted(str(f.resolve()) for f in frames))
        self.assertEqual(r["files"], [str(f.resolve()) for f in frames])

    def test_agy_without_answer_raises(self):
        empty = subprocess.CompletedProcess([], 1, b'{"status": "ERROR", "response": ""}', b"boom")
        with mock.patch("subprocess.run", return_value=empty):
            with self.assertRaises(server.Fail):
                server.run_agy("p", "m", [])

    def test_missing_file_is_an_error(self):
        with self.assertRaises(server.Fail):
            asyncio.run(server.gemini_analyze_media(str(self.root / "nope.mp4"), "x"))


class FileTests(Base):
    def test_pcm_rate(self):
        self.assertEqual(server.pcm_rate("audio/L16;codec=pcm;rate=24000"), 24000)
        self.assertEqual(server.pcm_rate("audio/L16; rate=16000"), 16000)
        self.assertEqual(server.pcm_rate(""), 24000)

    def test_write_wav(self):
        out = server.write_audio(b"\0\0" * 2400, 24000, self.root / "a.wav")
        with wave.open(str(out)) as w:
            self.assertEqual((w.getframerate(), w.getnframes()), (24000, 2400))

    def test_write_image_same_type(self):
        out = server.write_image(b"PNGDATA", "image/png", self.root / "i.png")
        self.assertEqual(out.read_bytes(), b"PNGDATA")

    def test_generation_needs_api_key(self):
        with mock.patch.dict(os.environ, {"GEMINI_API_KEY": ""}):
            with self.assertRaises(server.Fail) as e:
                server.genai_client()
        self.assertIn("set but empty", str(e.exception))
        env = {k: v for k, v in os.environ.items() if k != "GEMINI_API_KEY"}
        with mock.patch.dict(os.environ, env, clear=True):
            with self.assertRaises(server.Fail) as e:
                server.genai_client()
        self.assertIn("is not set", str(e.exception))

    def test_mp3_keeps_an_existing_wav_of_the_same_name(self):
        try:
            server.media.find_ffmpeg()
        except server.Fail:
            self.skipTest("no ffmpeg (pip install -e \".[media]\")")
        mine = self.root / "narration.wav"
        mine.write_bytes(b"my own recording")
        out = server.write_audio(b"\0\0" * 2400, 24000, self.root / "narration.mp3")
        self.assertEqual(out, self.root / "narration.mp3")
        self.assertTrue(out.exists())
        self.assertEqual(mine.read_bytes(), b"my own recording")
        self.assertEqual(sorted(p.name for p in self.root.iterdir()), ["narration.mp3", "narration.wav"])

    def test_without_ffmpeg_the_wav_gets_a_free_name(self):
        mine = self.root / "narration.wav"
        mine.write_bytes(b"my own recording")
        with mock.patch.object(server.media, "find_ffmpeg", side_effect=server.Fail("no ffmpeg")):
            out = server.write_audio(b"\0\0" * 2400, 24000, self.root / "narration.mp3")
        self.assertEqual(out.name, "narration-1.wav")
        self.assertEqual(mine.read_bytes(), b"my own recording")
        self.assertEqual(sorted(p.name for p in self.root.iterdir()), ["narration-1.wav", "narration.wav"])

    def test_tts_uses_v1beta(self):
        """The stable v1 API rejects speech_config (400 'Unknown name speechConfig')."""
        from google.genai import types
        pcm = b"\0\0" * 2400
        part = types.Part(inline_data=types.Blob(data=pcm, mime_type="audio/L16;codec=pcm;rate=24000"))
        resp = types.GenerateContentResponse(candidates=[types.Candidate(content=types.Content(parts=[part]))])
        with mock.patch("google.genai.Client") as client:
            client.return_value.models.generate_content.return_value = resp
            out, seconds, _ = server._generate_audio("hi", "Kore", self.root / "t.wav")
        self.assertEqual(client.call_args.kwargs["http_options"].api_version, "v1beta")
        self.assertEqual(seconds, 0.1)
        self.assertTrue(out.exists())


if __name__ == "__main__":
    unittest.main()
