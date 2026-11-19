"""Running the agy CLI as a child process."""
from __future__ import annotations

import json
import locale
import os
import shutil

# Keys that would let a child process bill the API instead of the subscription.
API_KEY_VARS = ("GEMINI_API_KEY", "GOOGLE_API_KEY")


def agy_exe() -> str:
    return os.environ.get("GEMINI_BUDDY_AGY") or shutil.which("agy") or "agy"


def agy_env() -> dict[str, str]:
    env = dict(os.environ)
    for k in API_KEY_VARS:
        env.pop(k, None)
    return env


def decode(raw: bytes) -> str:
    """agy writes UTF-8 to a terminal but the console code page (e.g. cp932) to a pipe on Windows."""
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode(locale.getpreferredencoding(False), "replace")


def parse_json_tail(text: str) -> dict | None:
    """agy may print warning lines before the JSON object; take the last JSON line."""
    if not text:
        return None
    for line in reversed(text.strip().splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                return json.loads(line)
            except json.JSONDecodeError:
                pass
    start = text.find("{")
    if start >= 0:
        try:
            return json.loads(text[start:])
        except json.JSONDecodeError:
            pass
    return None
