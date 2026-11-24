"""Image editing input, music and video generation with a fake google-genai client (no API key used)."""
import asyncio
import base64
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import server  # noqa: E402
from gemini_buddy import interact  # noqa: E402
from test_server import Base  # noqa: E402


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


class ImageInputTests(Base):
    def test_input_images_go_before_the_prompt(self):
        from google.genai import types
        src = self.root / "in.png"
        src.write_bytes(b"PNGIN")
        part = types.Part(inline_data=types.Blob(data=b"PNGOUT", mime_type="image/png"))
        resp = types.GenerateContentResponse(candidates=[types.Candidate(content=types.Content(parts=[part]))])
        client = mock.MagicMock()
        client.models.generate_content.return_value = resp
        with mock.patch.object(server, "genai_client", return_value=client):
            saved, *_ = server._generate_image_api("make it red", self.root / "out.png", "", [src],
                                                   aspect_ratio="16:9", image_size="2K")
        kw = client.models.generate_content.call_args.kwargs
        self.assertEqual(kw["contents"][0].inline_data.data, b"PNGIN")
        self.assertEqual(kw["contents"][1], "make it red")
        self.assertEqual((kw["config"].image_config.aspect_ratio, kw["config"].image_config.image_size),
                         ("16:9", "2K"))
        self.assertEqual(saved.read_bytes(), b"PNGOUT")

    def test_only_images_are_inputs(self):
        clipf = self.root / "a.mp4"
        clipf.write_bytes(b"x")
        with self.assertRaises(server.Fail):
            server.image_parts([clipf])

    def test_agy_backend_refuses_inputs(self):
        src = self.root / "in.png"
        src.write_bytes(b"x")
        with self.assertRaises(server.Fail) as e:
            asyncio.run(server.gemini_generate_image("x", str(self.root / "o.png"), backend="agy",
                                                     input_paths=[str(src)]))
        self.assertIn("backend='api'", str(e.exception))


class InputBlockTests(Base):
    def test_image_inline_video_uploaded(self):
        img, vid = self.root / "a.jpg", self.root / "b.mp4"
        img.write_bytes(b"JPG")
        vid.write_bytes(b"MP4")
        client = mock.MagicMock()
        client.files.upload.return_value = NS(name="files/1", uri="https://x/files/1", state=NS(name="PROCESSING"))
        client.files.get.return_value = NS(name="files/1", uri="https://x/files/1", state=NS(name="ACTIVE"))
        with mock.patch.object(interact.time, "sleep"):
            blocks = interact.input_blocks(client, [img, vid], "go")
        self.assertEqual(blocks[0], {"type": "image", "mime_type": "image/jpeg", "data": b64(b"JPG")})
        self.assertEqual(blocks[1], {"type": "video", "mime_type": "video/mp4", "uri": "https://x/files/1"})
        self.assertEqual(blocks[2], {"type": "text", "text": "go"})

    def test_no_files_is_plain_text(self):
        self.assertEqual(interact.input_blocks(mock.MagicMock(), [], "hi"), "hi")

    def test_unsupported_type(self):
        with self.assertRaises(server.Fail):
            interact.kind_of(Path("notes.txt"))

    def test_uri_output_is_downloaded(self):
        client = mock.MagicMock()
        client.files.download.return_value = b"VIDEO"
        data, mime = interact.content_bytes(client, NS(data=None, uri="files/9", mime_type="video/mp4"))
        self.assertEqual((data, mime), (b"VIDEO", "video/mp4"))


def interaction(status="completed", **kw):
    base = dict(id="v1_abc", status=status, output_audio=None, output_video=None, output_text="",
                usage=None, errors=None)
    base.update(kw)
    return NS(**base)


class MusicTests(Base):
    def run_music(self, out_name, model=None):
        client = mock.MagicMock()
        client.interactions.create.return_value = interaction(
            output_audio=NS(data=b64(b"MP3DATA"), uri=None, mime_type="audio/mp3"), output_text="[Verse] la")
        with mock.patch.object(server, "genai_client", return_value=client) as make, \
                mock.patch.object(server.media, "duration", return_value=30.0):
            r = asyncio.run(server.gemini_generate_music("calm piano", str(self.root / out_name), model=model))
        make.assert_called_with("v1beta")  # the Interactions API is not on v1 (404 there)
        return r, client.interactions.create.call_args.kwargs

    def test_mp3_saved_as_sent(self):
        r, kw = self.run_music("bgm.mp3")
        self.assertEqual(Path(r["path"]).read_bytes(), b"MP3DATA")
        self.assertEqual(kw["response_format"], {"type": "audio"})
        self.assertEqual((r["seconds"], r["notes"]), (30.0, "[Verse] la"))
        s = server.open_session(r["session_id"], "")
        self.assertEqual([a.kind for a in s.artifacts], ["music"])

    def test_wav_is_asked_for(self):
        _, kw = self.run_music("bgm.wav")
        self.assertEqual(kw["response_format"], {"type": "audio", "mime_type": "audio/wav"})

    def test_clip_model_gets_no_response_format(self):
        _, kw = self.run_music("bgm.mp3", model="lyria-3-clip-preview")
        self.assertNotIn("response_format", kw)

    def test_failure_is_reported(self):
        client = mock.MagicMock()
        client.interactions.create.return_value = interaction(status="failed", errors=[NS(message="blocked")])
        with mock.patch.object(server, "genai_client", return_value=client):
            with self.assertRaises(server.Fail) as e:
                server._generate_music("x", self.root / "m.mp3", "lyria-3.5")
        self.assertIn("blocked", str(e.exception))


class VideoTests(Base):
    def setUp(self):
        super().setUp()
        self.client = mock.MagicMock()
        self.client.interactions.create.return_value = interaction(status="in_progress")
        for p in (mock.patch.object(server, "genai_client", return_value=self.client),
                  mock.patch.object(server.time, "sleep"),
                  mock.patch.object(server.media, "duration", return_value=10.0)):
            p.start()
            self.addCleanup(p.stop)

    def done(self):
        return interaction(output_video=NS(data=b64(b"MP4DATA"), uri=None, mime_type="video/mp4"))

    def test_starts_in_background_and_saves_when_done(self):
        self.client.interactions.get.side_effect = [interaction(status="in_progress"), self.done()]
        out = self.root / "v.mp4"
        r = asyncio.run(server.gemini_generate_video("a cat walks", str(out), resolution="360p",
                                                     duration_seconds=5, wait_seconds=60))
        kw = self.client.interactions.create.call_args.kwargs
        self.assertTrue(kw["background"])
        self.assertTrue(all(c.args == ("v1beta",) for c in server.genai_client.call_args_list))
        self.assertEqual(kw["response_format"],
                         {"type": "video", "aspect_ratio": "16:9", "resolution": "360p", "duration": "5s"})
        self.assertEqual((r["status"], r["interaction_id"], r["seconds"]), ("done", "v1_abc", 10.0))
        self.assertEqual(out.read_bytes(), b"MP4DATA")
        s = server.open_session(r["session_id"], "")
        self.assertEqual([a.kind for a in s.artifacts], ["video"])

    def test_running_then_status_finishes_it(self):
        self.client.interactions.get.return_value = interaction(status="in_progress")
        r = asyncio.run(server.gemini_generate_video("x", str(self.root / "v.mp4"), wait_seconds=0))
        self.assertEqual(r["status"], "running")
        self.assertIn("gemini_video_status", r["next"])
        self.client.interactions.get.return_value = self.done()
        r2 = asyncio.run(server.gemini_video_status(r["interaction_id"], wait_seconds=0))
        self.assertEqual(r2["status"], "done")
        again = asyncio.run(server.gemini_video_status(r["interaction_id"]))  # no new API call
        self.assertEqual(again["path"], r2["path"])
        self.assertEqual(self.client.interactions.get.call_count, 2)
        s = server.open_session(r["session_id"], "")
        self.assertEqual(len(s.artifacts), 1)

    def test_edit_passes_previous_interaction(self):
        self.client.interactions.get.return_value = self.done()
        asyncio.run(server.gemini_generate_video("make it night", str(self.root / "v2.mp4"),
                                                 previous_interaction_id="v1_prev", task="edit"))
        kw = self.client.interactions.create.call_args.kwargs
        self.assertEqual(kw["previous_interaction_id"], "v1_prev")
        self.assertEqual(kw["generation_config"], {"video_config": {"task": "edit"}})

    def test_failed_job(self):
        self.client.interactions.get.return_value = interaction(status="failed", errors=[NS(message="safety")])
        r = asyncio.run(server.gemini_generate_video("x", str(self.root / "v.mp4")))
        self.assertEqual((r["status"], r["error"]), ("failed", "safety"))

    def test_bad_duration(self):
        with self.assertRaises(server.Fail):
            asyncio.run(server.gemini_generate_video("x", str(self.root / "v.mp4"), duration_seconds=20))
        self.client.interactions.create.assert_not_called()

    def test_cancel(self):
        self.client.interactions.get.return_value = interaction(status="in_progress")
        r = asyncio.run(server.gemini_generate_video("x", str(self.root / "v.mp4"), wait_seconds=0))
        r2 = asyncio.run(server.gemini_video_status(r["interaction_id"], cancel=True))
        self.client.interactions.cancel.assert_called_once()
        self.assertEqual(r2["status"], "cancelled")


class WriteMediaTests(Base):
    def test_same_extension_written_directly(self):
        out = server.write_media(b"abc", ".mp3", self.root / "m.mp3")
        self.assertEqual(out.read_bytes(), b"abc")

    def test_no_ffmpeg_keeps_the_real_type(self):
        with mock.patch.object(server.media, "find_ffmpeg", side_effect=server.Fail("no ffmpeg")):
            out = server.write_media(b"abc", ".mp3", self.root / "m.wav")
        self.assertEqual((out.name, out.read_bytes()), ("m.mp3", b"abc"))


if __name__ == "__main__":
    unittest.main()
