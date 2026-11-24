"""Helpers for the Gemini Interactions API (client.interactions): music and video generation.

Input files become content blocks: small images are sent inline (base64); videos, audio and
large images are uploaded with the Files API first (kept 48 h by Google) and sent by URI.
Outputs arrive as base64 `data` or, for big files, a `uri` that the Files API downloads.
"""
from __future__ import annotations

import base64
import time
from pathlib import Path
from typing import Any

from .errors import Fail

IMAGE_MIME = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp",
              ".heic": "image/heic", ".heif": "image/heif"}
VIDEO_MIME = {".mp4": "video/mp4", ".mov": "video/mov", ".webm": "video/webm", ".mpeg": "video/mpeg",
              ".mpg": "video/mpg", ".avi": "video/avi"}
AUDIO_MIME = {".wav": "audio/wav", ".mp3": "audio/mp3", ".m4a": "audio/m4a", ".aac": "audio/aac",
              ".ogg": "audio/ogg", ".flac": "audio/flac"}
# extension to save an output under, by the MIME type the API sent
OUT_EXT = {"video/mp4": ".mp4", "video/webm": ".webm", "video/mov": ".mov", "video/quicktime": ".mov",
           "audio/mp3": ".mp3", "audio/mpeg": ".mp3", "audio/wav": ".wav", "audio/x-wav": ".wav",
           "audio/ogg": ".ogg", "audio/ogg_opus": ".ogg", "audio/flac": ".flac", "audio/m4a": ".m4a",
           "audio/aac": ".aac"}
INLINE_IMAGE_BYTES = 7 * 1024 * 1024   # bigger images go through the Files API (inline cap is 20 MB per request)
UPLOAD_WAIT = 180.0                     # seconds to wait for an uploaded file to become ACTIVE
DONE = "completed"
FAILED = ("failed", "cancelled", "incomplete", "budget_exceeded")


def kind_of(path: Path) -> tuple[str, str]:
    """('image' | 'video' | 'audio', mime) for a supported input file, else Fail."""
    ext = path.suffix.lower()
    for kind, table in (("image", IMAGE_MIME), ("video", VIDEO_MIME), ("audio", AUDIO_MIME)):
        if ext in table:
            return kind, table[ext]
    raise Fail(f"unsupported input file type {ext or '(none)'}: {path.name} "
               f"(images {', '.join(IMAGE_MIME)}; videos {', '.join(VIDEO_MIME)}; audio {', '.join(AUDIO_MIME)})")


def upload(client, path: Path, mime: str, wait: float = UPLOAD_WAIT) -> str:
    """Upload with the Files API and wait until it is ACTIVE. Returns the file's URI."""
    try:
        f = client.files.upload(file=str(path), config={"mime_type": mime})
    except Exception as e:
        raise Fail(f"upload of {path.name} failed: {e}")
    deadline = time.time() + wait
    while _state(f) == "PROCESSING":
        if time.time() > deadline:
            raise Fail(f"{path.name} was uploaded but is still processing after {int(wait)} s; try again later")
        time.sleep(3)
        f = client.files.get(name=f.name)
    if _state(f) != "ACTIVE":
        raise Fail(f"upload of {path.name} ended in state {_state(f)}")
    return f.uri


def _state(f) -> str:
    s = getattr(f, "state", None)
    return str(getattr(s, "name", s) or "ACTIVE").upper()


def input_blocks(client, paths: list[Path], prompt: str) -> list[dict[str, Any]] | str:
    """Content blocks for the files, then the prompt text. Just the text when there are no files."""
    if not paths:
        return prompt
    blocks: list[dict[str, Any]] = []
    for p in paths:
        kind, mime = kind_of(p)
        if kind == "image" and p.stat().st_size <= INLINE_IMAGE_BYTES:
            blocks.append({"type": "image", "mime_type": mime,
                           "data": base64.b64encode(p.read_bytes()).decode("ascii")})
        else:
            blocks.append({"type": kind, "mime_type": mime, "uri": upload(client, p, mime)})
    blocks.append({"type": "text", "text": prompt})
    return blocks


def content_bytes(client, content) -> tuple[bytes, str]:
    """(bytes, mime) of an output content block (inline data, or a URI the Files API downloads)."""
    mime = str(getattr(content, "mime_type", "") or "")
    data = getattr(content, "data", None)
    if data:
        return (data if isinstance(data, bytes) else base64.b64decode(data)), mime
    uri = getattr(content, "uri", None)
    if uri:
        try:
            got = client.files.download(file=uri)
        except Exception as e:
            raise Fail(f"could not download the generated file {uri}: {e}")
        if not got:
            raise Fail(f"downloading {uri} returned nothing")
        return got, mime
    raise Fail("the output has neither data nor a URI")


def out_ext(mime: str, fallback: str) -> str:
    return OUT_EXT.get(mime.split(";")[0].strip().lower(), fallback)


def usage(interaction) -> dict[str, Any] | None:
    u = getattr(interaction, "usage", None)
    if u is None:
        return None
    keys = ("total_input_tokens", "total_output_tokens", "total_tokens")
    return {k: getattr(u, k) for k in keys if getattr(u, k, None) is not None} or None


def errors_text(interaction) -> str:
    errs = getattr(interaction, "errors", None) or []
    parts = [getattr(e, "message", None) or str(e) for e in errs]
    return "; ".join(p for p in parts if p) or (getattr(interaction, "output_text", None) or "")
