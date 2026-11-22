"""Running the agy CLI as a child process."""
from __future__ import annotations

import json
import locale
import os
import shutil

# Keys that would let a child process bill the API instead of the subscription.
API_KEY_VARS = ("GEMINI_API_KEY", "GOOGLE_API_KEY")


# Passed to every `agy -p` with --settings (this run only, merged over the user's settings.json;
# deny wins over the user's allow). Headless agy still runs commands the user's settings allow,
# even outside its sandbox when the sandbox fails to start (seen with agy 1.5.14), so deny them
# all here, and file writes too (agy's image generator saves its image without write_file).
STRICT_SETTINGS = {"permissions": {"allow": [], "deny": ["command(*)", "write_file(*)"], "ask": []}}


def strict_settings_arg() -> list[str]:
    return ["--settings", json.dumps(STRICT_SETTINGS, separators=(",", ":"))]


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
