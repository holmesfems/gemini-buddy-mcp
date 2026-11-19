"""Local media tools on synthetic media made with ffmpeg (no model calls).

Skipped when ffmpeg / Pillow / numpy / OpenCV are missing (pip install -e ".[media]").
"""
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
from gemini_buddy import measure, media  # noqa: E402
from gemini_buddy.errors import Fail  # noqa: E402

try:
    FF = media.find_ffmpeg()
    import cv2  # noqa: F401
    import numpy as np
    from PIL import Image
except Exception:  # noqa: BLE001
    FF = None


def ff(*args):
    subprocess.run([FF, "-hide_banner", "-loglevel", "error", "-y", *args], check=True, capture_output=True)


@unittest.skipUnless(FF, "media extras not installed")
class MediaTools(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.tmp.name).resolve()
        cls.video = cls.root / "v.mp4"
        # 640x360, 4 s; a white 40x40 square moves right 40 px per second on black; 440 Hz tone
        ff("-f", "lavfi", "-i", "color=c=black:s=640x360:r=25:d=4",
           "-f", "lavfi", "-i", "color=c=white:s=40x40:r=25:d=4",
           "-f", "lavfi", "-i", "sine=frequency=440:duration=4",
           "-filter_complex", "[0][1]overlay=x='100+40*t':y=150[v]", "-map", "[v]", "-map", "2:a",
           "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(cls.video))
        # voice: 1 s tone, 1 s silence, 1 s tone, 1 s silence; mix = voice + quiet noise
        cls.voice = cls.root / "voice.wav"
        ff("-f", "lavfi", "-i", "sine=frequency=300:duration=4", "-af",
           "volume='if(lt(mod(t,2),1),1,0)':eval=frame", "-ar", "16000", "-ac", "1", str(cls.voice))
        cls.mix = cls.root / "mix.wav"
        ff("-i", str(cls.voice), "-f", "lavfi", "-i", "anoisesrc=color=pink:amplitude=0.02:duration=4",
           "-filter_complex", "[0][1]amix=inputs=2:normalize=0", "-ar", "16000", "-ac", "1", str(cls.mix))

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_clip(self):
        r = media.cut_clip(str(self.video), self.root / "c.mp4", 1, 3, height=180, fps=10)
        self.assertGreater(r["bytes"], 0)
        with self.assertRaises(Fail):
            media.cut_clip(str(self.video), self.root / "bad.mp4", 3, 1)

    def test_frames_grid_sheet_and_overlay(self):
        out = self.root / "fr"
        r = media.frames(str(self.video), out, start=1, end=3, fps=2, crop="320:200:100:100", grid=50,
                         sheet=True)
        self.assertEqual(r["frames"], 4)
        idx = json.loads(Path(r["index"]).read_text(encoding="utf-8"))
        self.assertEqual(idx["crop"], {"x": 100, "y": 100})
        self.assertEqual([f["t_video"] for f in idx["frames"]], [1.0, 1.5, 2.0, 2.5])
        with Image.open(r["first"]) as im:
            self.assertEqual(im.size, (320, 200))
        self.assertTrue(Path(r["sheet"]).exists())
        # square centre at t: x = 100 + 40 t + 20, y = 170 (source pixels)
        pts = [{"t": f["t_video"], "x": 120 + 40 * f["t_video"], "y": 170} for f in idx["frames"]]
        o = media.overlay(r["index"], [pts, [{"frame": "f_001.png", "x": 5, "y": 5}]], self.root / "chk.png",
                          space="video")
        self.assertEqual(o["points_drawn"][0]["frames_with_point"], 4)
        self.assertEqual(o["points_drawn"][1]["frames_with_point"], 1)
        self.assertTrue(Path(o["path"]).exists())

    def test_audio_stats(self):
        r = media.audio(str(self.voice), self.root / "au", start=0, end=4, silence_db=-40, silence_min=0.3)
        self.assertTrue(r["loudness"])
        spans = r["sound_spans"]
        self.assertEqual(len(spans), 2)
        self.assertAlmostEqual(spans[0][0], 0.0, delta=0.1)
        self.assertAlmostEqual(spans[1][0], 2.0, delta=0.1)
        for k in ("audio.wav", "wave.png", "spectrum.png"):
            self.assertTrue(Path(r["files"][k]).exists())

    def test_mix_balance(self):
        r = measure.mix_balance(str(self.mix), str(self.voice), start=0, end=4, voice_start=0,
                                out=self.root / "bal.json")
        self.assertAlmostEqual(r["voice_gain"], 1.0, delta=0.05)
        self.assertGreater(r["mean_voice_minus_bgm_db"], 20)  # tone at full scale vs pink noise at 0.02
        self.assertEqual(r["buried_spans"], [])
        self.assertTrue(json.loads((self.root / "bal.json").read_text(encoding="utf-8"))["rows"])

    def test_track_follows_the_square(self):
        # the box takes in some black around the square: a flat template cannot be matched
        with self.assertRaises(Fail):
            measure.track(str(self.video), 0, 1, "105,155,30,30", self.root / "flat.json")
        r = measure.track(str(self.video), 0, 3, "95,145,50,50", self.root / "t.json")
        last = r["last"]
        self.assertAlmostEqual(last["x"], 120 + 40 * last["t"], delta=6)
        self.assertAlmostEqual(last["y"], 170, delta=3)
        with self.assertRaises(Fail):
            measure.track(str(self.video), 0, 1, "630,350,40,40", self.root / "t2.json")

    def test_frames_sort_by_number_past_999(self):
        names = ["f_1000.png", "f_101.png", "f_999.png", "f_100.png", "f_001.png"]
        got = [p.name for p in sorted((Path(n) for n in names), key=media.frame_number)]
        self.assertEqual(got, ["f_001.png", "f_100.png", "f_101.png", "f_999.png", "f_1000.png"])

    def test_mix_balance_short_range_finds_a_late_voice(self):
        # the voice starts 1 s into the mix; the range is 4 s (shorter than the 10 s search piece)
        late = self.root / "late.wav"
        ff("-i", str(self.voice), "-f", "lavfi", "-i", "anoisesrc=color=pink:amplitude=0.02:duration=5",
           "-filter_complex", "[0]adelay=1000[v];[v][1]amix=inputs=2:normalize=0:duration=longest",
           "-ar", "16000", "-ac", "1", str(late))
        r = measure.mix_balance(str(late), str(self.voice), start=0, end=4, voice_start=0)
        self.assertAlmostEqual(r["voice_track_offset_s"], -1.0, delta=0.01)  # mix 1.0 s = voice 0.0 s
        self.assertAlmostEqual(r["voice_gain"], 1.0, delta=0.05)
        self.assertGreater(r["mean_voice_minus_bgm_db"], 20)
        self.assertIn("at mix time t the voice track is at t + voice_track_offset_s", r["note"])

    def test_mix_balance_short_files_without_voice_start(self):
        # the mix starts 1 s into the 4 s voice track and is as long as it (4 s): with the whole
        # mix as the search piece there was no room to slide it, so the offset was stuck at 0
        early = self.root / "early.wav"
        ff("-ss", "1", "-i", str(self.voice), "-f", "lavfi", "-i",
           "anoisesrc=color=pink:amplitude=0.02:duration=4", "-filter_complex",
           "[0][1]amix=inputs=2:normalize=0:duration=longest", "-ar", "16000", "-ac", "1", str(early))
        r = measure.mix_balance(str(early), str(self.voice), start=0, end=4)
        self.assertAlmostEqual(r["voice_track_offset_s"], 1.0, delta=0.01)  # mix 0 s = voice 1 s
        self.assertAlmostEqual(r["voice_gain"], 1.0, delta=0.05)
        self.assertGreater(r["mean_voice_minus_bgm_db"], 20)

    def test_overlay_takes_geminis_answer_as_is(self):
        """Through the MCP layer, so the argument types are checked as a client would send them."""
        with mock.patch.dict(os.environ, {"GEMINI_BUDDY_HOME": str(self.root / "home2")}):
            fr = media.frames(str(self.video), self.root / "fr2", start=0, end=1, fps=2)
            answer = {"track": [{"frame": "f_001.png", "x": 120, "y": 170},
                                {"frame": "f_002.png", "x": 140, "y": 170}]}
            res = asyncio.run(server.mcp.call_tool("media_overlay", {"index_path": fr["index"],
                                                                     "points": [answer]}))
        text = res.content[0].text if hasattr(res, "content") else res[0][0].text
        self.assertEqual(json.loads(text)["points_drawn"][0]["frames_with_point"], 2)

    def test_tools_record_into_a_session(self):
        with mock.patch.dict(os.environ, {"GEMINI_BUDDY_HOME": str(self.root / "home")}):
            s = server.open_session(None, "prep")
            server.save_session(s)
            r = asyncio.run(server.media_frames(str(self.video), start=0, end=1, session_id=s.session_id))
            self.assertIn(str(self.root / "home" / "work" / s.session_id), r["index"])
            listed = server.gemini_list_sessions()["sessions"][0]
            self.assertIn(r["index"], listed["files"])


if __name__ == "__main__":
    unittest.main()
