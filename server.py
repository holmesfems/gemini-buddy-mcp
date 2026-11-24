"""gemini-buddy-mcp: a stdio MCP server that pairs Claude with Gemini.

Two backends, chosen per tool:

- Consult / media analysis / code review -> the `agy` CLI (Antigravity, signed in
  with the subscription). `GEMINI_API_KEY` / `GOOGLE_API_KEY` are removed from the
  child's environment so these calls never fall through to API billing.
- Delegated coding tasks (gemini_delegate_*) -> one `agy acp` per task in its own git
  worktree, with agy's own settings swapped for strict ones (see gemini_buddy/delegate.py).
- Image / speech / music / video generation -> the `google-genai` SDK with `GEMINI_API_KEY`
  (the monthly API credit). Images can also be made through agy (backend="agy").
  Music and video use the Interactions API (gemini_buddy/interact.py); video runs in the
  background and is fetched with gemini_video_status.

Local tools (ffmpeg, Pillow, numpy, OpenCV) prepare inputs for agy and measure what a
model cannot judge reliably: clips, frames with a coordinate grid, audio stats,
voice-vs-BGM balance, object tracking, overlays to check positions by eye.

Every tool takes an optional `session_id`. Sessions live in
`~/.gemini-buddy-mcp/sessions/<session_id>.json` and keep the conversation, media
summaries and generated files, so a later call can pick the thread up.
Tools return text and file paths, never binary data.
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import tempfile
import time
import uuid
import wave
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

import anyio
from pydantic import BaseModel, Field

from gemini_buddy import acp, delegate, interact, measure, media
from gemini_buddy import review as review_mod
from gemini_buddy.agyproc import (API_KEY_VARS, agy_env, agy_exe, decode, parse_json_tail,  # noqa: F401
                                  strict_settings_arg)
from gemini_buddy.errors import Fail

try:  # mcp 2.x renamed FastMCP to MCPServer
    from mcp.server.mcpserver import MCPServer as FastMCP
except ImportError:  # mcp 1.x
    from mcp.server.fastmcp import FastMCP

log = logging.getLogger("gemini-buddy-mcp")

# ---------------------------------------------------------------- settings

AGY = agy_exe()
CONSULT_MODEL = os.environ.get("GEMINI_BUDDY_CONSULT_MODEL", "gemini-4-neon-high")
MEDIA_MODEL = os.environ.get("GEMINI_BUDDY_MEDIA_MODEL", "gemini-4-hydrogen-high")
IMAGE_MODEL = os.environ.get("GEMINI_BUDDY_IMAGE_MODEL", "gemini-nano-banana-2.1")
TTS_MODEL = os.environ.get("GEMINI_BUDDY_TTS_MODEL", "gemini-3.8-flash-tts")
MUSIC_MODEL = os.environ.get("GEMINI_BUDDY_MUSIC_MODEL", "lyria-3.5")
VIDEO_MODEL = os.environ.get("GEMINI_BUDDY_VIDEO_MODEL", "gemini-omni-1.2-flash")
AGY_IMAGE_MODEL = os.environ.get("GEMINI_BUDDY_AGY_IMAGE_MODEL", "gemini-4-hydrogen-high")
AGY_TIMEOUT = int(os.environ.get("GEMINI_BUDDY_AGY_TIMEOUT", "600"))

RECENT_TURNS = 6          # turns quoted back into the next prompt
TURN_CHARS = 1200         # per quoted turn
SUMMARY_CHARS = 2000      # media summary kept in the session
RESPONSE_CHARS = 20000    # cap on text returned to the client
CONTEXT_TOOLS = ("gemini_consult", "gemini_analyze_media", "gemini_review")

GUARD = (
    "Rules for this session: you have NO terminal access (any shell command is denied and "
    "ends your turn). Do not search the web. Open only the files named in the task. "
    "Use your file-reading tool to see images and hear audio. Do not edit any file. "
    "When done, write the answer directly.\n\n"
)
IMAGE_GUARD = (
    "Rules for this session: you have NO terminal access (any shell command is denied and "
    "ends your turn). Do not search the web. Do not open or edit any file. Use your built-in "
    "image generation tool to make exactly one image, then reply with one short sentence.\n\n"
)

INSTRUCTIONS = """\
Gemini を相棒にするツール群。Claude にできない・苦手なこと（画像や音声を作る、音を聞く、
画素の座標を読む、別のモデルの目でのレビュー）を補う。

- gemini_* の相談・解析・レビューは agy（サブスク）、生成は API クレジット。どれも session_id で続きを頼める。
- 生成: 画像 gemini_generate_image（input_paths に元画像を渡すと編集・参照）、読み上げ gemini_generate_audio、
  音楽 gemini_generate_music、動画 gemini_generate_video（裏で走る。status が running なら gemini_video_status で待つ。
  720p で 1 秒およそ $0.07 なので、試しは 360p で）。できた音や動画は自分では確かめられないので、解析ツールかユーザーに頼む。
- agy は端末を使えない。長い動画は media_clip / media_frames / media_audio で先に切って渡す（30〜60 秒、720p）。
- agy の答えはそのまま信じない:
  - 画像やコマは自分で Read して確かめる。座標は media_overlay で描いて見る。
  - 時刻は media_audio の sound_spans（無音区間から出した発話の区切り）と突き合わせる。
  - BGM の音量は media_mix_balance の数値で判断する（モデルの「声が埋もれている」は外れやすい）。
  - 発音の違和感の指摘は候補として扱い、ユーザーに聞いて確かめてもらう。
  - gemini_review の指摘は 1 件ずつ実際のコードでたどってから報告する。agreed が多くても確かめずに本物扱いしない。
- モデル: 音声・動画は gemini-4-hydrogen-high（速い）、座標・コードレビューは gemini-4-neon-high（正確）。
- gemini_review の intent には作者の意図だけを書く（Claude の意見を混ぜない）。STOPPED が返ったらその行をユーザーに見せる。
- gemini_delegate_start は作業を丸ごと任せる（新しい git worktree で、agy は worktree の中だけ読み書きできる）。
  start のあとは gemini_delegate_status（wait_seconds で待てる）。マージはしない: 終わったら changes の差分を
  自分で読んで確かめてから取り込む（agy の「テストした」「直した」は主張として扱う）。直してほしい点は
  gemini_delegate_reply、要らなくなった worktree は gemini_delegate_cancel(discard=True)。
  status が stopped なら warnings をユーザーに見せる（agy が許可なしに動こうとした印）。

頼み方の例（gemini_analyze_media の prompt）:
- 文字起こし（media_audio の audio.wav、30〜60 秒ずつ）: "Listen once. Answer a transcript, one utterance per
  line: [start-end seconds] A or B: text. Then a line ISSUES: words that sound mispronounced (time + word), or none."
- BGM が場面に合うか（media_clip の mp4）: まず見えた・聞こえた証拠（冒頭のロゴの文字、画面のタイトル、最初のセリフ）を
  書かせ、次に BGM のジャンル・テンポ・雰囲気・楽器、場面に合うか（1〜5 と理由）、不自然なつなぎ目、直し方の案。場面の説明も添える。
- 座標（media_frames の grid=100 のコマを数枚、model=gemini-4-neon-high）: 対象を見た目と最初のコマでのだいたいの位置で指定し、
  "read the grid labels; answer JSON only: {"track": [{"frame": "f_001.png", "x": .., "y": ..}]}; null if not visible"。
  答えの {"track": [...]} をそのまま media_overlay の points の 1 要素として渡して（points=[答え]）確かめる。
"""


def home() -> Path:
    return Path(os.environ.get("GEMINI_BUDDY_HOME") or Path.home() / ".gemini-buddy-mcp")


def sessions_dir() -> Path:
    return home() / "sessions"


def now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def clip(text: str, limit: int) -> str:
    text = text or ""
    return text if len(text) <= limit else text[:limit] + f"\n…[{len(text) - limit} chars cut]"


# ---------------------------------------------------------------- sessions

class Turn(BaseModel):
    tool: str
    at: str
    prompt: str
    response: str = ""
    files: list[str] = Field(default_factory=list)
    model: str | None = None
    agy_conversation_id: str | None = None


class MediaNote(BaseModel):
    file: str
    more_files: list[str] = Field(default_factory=list)
    prompt: str
    summary: str
    at: str


class Artifact(BaseModel):
    kind: str  # image, audio, music, video, clip, frames, audio_stats, mix_balance, track, overlay, review
    path: str
    prompt: str
    model: str = ""
    at: str
    usage: dict[str, Any] | None = None


class Session(BaseModel):
    session_id: str
    title: str
    created_at: str
    updated_at: str
    turns: list[Turn] = Field(default_factory=list)
    media: list[MediaNote] = Field(default_factory=list)
    artifacts: list[Artifact] = Field(default_factory=list)

    def files(self) -> list[str]:
        seen: dict[str, None] = {}
        for t in self.turns:
            for f in t.files:
                seen[f] = None
        for m in self.media:
            for f in [m.file, *m.more_files]:
                seen[f] = None
        for a in self.artifacts:
            seen[a.path] = None
        return list(seen)


def valid_session_id(session_id: str) -> str:
    """Session ids are UUIDs; anything else could escape the sessions folder."""
    try:
        return str(uuid.UUID(session_id))
    except (ValueError, AttributeError, TypeError):
        raise Fail(f"session_id must be a UUID returned by an earlier call, got {session_id!r}")


def session_path(session_id: str) -> Path:
    return sessions_dir() / f"{valid_session_id(session_id)}.json"


def make_title(text: str) -> str:
    line = " ".join((text or "").split())
    return line if len(line) <= 60 else line[:59] + "…"


def open_session(session_id: str | None, first_prompt: str) -> Session:
    """Load a session, or start a new one titled after the first prompt."""
    if session_id:
        path = session_path(session_id)
        if not path.exists():
            raise Fail(f"no session {session_id}; call gemini_list_sessions to see saved ones")
        return Session.model_validate_json(path.read_text(encoding="utf-8"))
    t = now()
    return Session(session_id=str(uuid.uuid4()), title=make_title(first_prompt), created_at=t, updated_at=t)


def save_session(s: Session) -> None:
    s.updated_at = now()
    path = session_path(s.session_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(s.model_dump_json(indent=2))
    os.replace(tmp, path)


LOCK_WAIT = 10.0   # seconds to wait for another call to finish saving the same session

if sys.platform == "win32":
    import msvcrt

    def _try_lock(f) -> bool:
        f.seek(0)
        try:
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
            return True
        except OSError:
            return False

    def _unlock(f) -> None:
        f.seek(0)
        msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
else:
    import fcntl

    def _try_lock(f) -> bool:
        try:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError:
            return False

    def _unlock(f) -> None:
        fcntl.flock(f.fileno(), fcntl.LOCK_UN)


@contextmanager
def session_lock(session_id: str):
    """An OS file lock on `<session>.lock`, so concurrent calls (other threads or other server
    processes) save one after another instead of overwriting each other. The OS releases it if a
    process dies, so a crash never leaves the session locked; the file itself just stays."""
    lock = session_path(session_id).with_suffix(".lock")
    lock.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.time() + LOCK_WAIT
    with open(lock, "a+b") as f:
        while not _try_lock(f):
            if time.time() > deadline:
                raise Fail(f"session {session_id} is busy (another call is saving it); try again")
            time.sleep(0.05)
        try:
            yield
        finally:
            _unlock(f)


def update_session(s: Session, change) -> Session:
    """Apply `change` to the latest saved copy of `s` and save it, under the session lock.

    Tools load a session before a long model call; re-reading here keeps what other calls
    saved in the meantime. A session that was never saved starts from `s` itself.
    """
    with session_lock(s.session_id):
        path = session_path(s.session_id)
        latest = (Session.model_validate_json(path.read_text(encoding="utf-8")) if path.exists()
                  else s.model_copy(deep=True))
        change(latest)
        save_session(latest)
    return latest


def require_session(session_id: str | None) -> None:
    """Fail before any slow work when a given session_id does not exist."""
    if session_id:
        open_session(session_id, "")


def session_context(s: Session) -> str:
    """Recent turns plus the standing notes (media summaries, generated files)."""
    made = [a for a in s.artifacts if a.kind in ("image", "audio", "music", "video")]
    turns = [t for t in s.turns if t.tool in CONTEXT_TOOLS][-RECENT_TURNS:]
    if not (turns or s.media or made):
        return ""
    out = [f"## Earlier in this session ({s.title})"]
    if s.media:
        out.append("### Media already analysed")
        for m in s.media[-10:]:
            more = f" (+{len(m.more_files)} more)" if m.more_files else ""
            out.append(f"- {m.file}{more} (asked: {clip(m.prompt, 200)})\n  {clip(m.summary, 600)}")
    if made:
        out.append("### Files generated")
        for a in made[-10:]:
            out.append(f"- {a.kind}: {a.path} (prompt: {clip(a.prompt, 200)})")
    if turns:
        out.append("### Recent exchanges")
        for t in turns:
            files = f" [files: {', '.join(t.files)}]" if t.files else ""
            out.append(f"**User ({t.tool}){files}:** {clip(t.prompt, TURN_CHARS)}\n"
                       f"**You:** {clip(t.response, TURN_CHARS)}")
    return "\n\n".join(out) + "\n\n## New request\n"


def work_dir(name: str, out_dir: str | None, session_id: str | None) -> Path:
    """Where a local tool writes: out_dir if given, else a fresh folder under the session (or _misc)."""
    if out_dir:
        p = Path(out_dir).expanduser().resolve()
    else:
        owner = valid_session_id(session_id) if session_id else "_misc"
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        # a random suffix: two calls in the same instant must not share (and clean) one folder
        p = home() / "work" / owner / f"{name}-{stamp}-{uuid.uuid4().hex[:8]}"
        p.mkdir(parents=True)
        return p
    p.mkdir(parents=True, exist_ok=True)
    return p


def record(session_id: str | None, kind: str, path: str, note: str) -> None:
    """Remember a local tool's output in the session, when one was given."""
    if not session_id:
        return
    update_session(open_session(session_id, note),
                   lambda x: x.artifacts.append(Artifact(kind=kind, path=path, prompt=note, at=now())))


# ---------------------------------------------------------------- agy backend

def agy_command(prompt: str, model: str, dirs: list[Path], mode: str = "plan") -> list[str]:
    # Never --dangerously-skip-permissions: headless agy may only read (or use its own generators).
    cmd = [AGY, "-p", prompt, "--model", model, "--mode", mode,
           "--output-format", "json", "--print-timeout", f"{AGY_TIMEOUT}s", *strict_settings_arg()]
    for d in dict.fromkeys(str(p) for p in dirs):
        cmd += ["--add-dir", d]
    return cmd


def agy_workspace() -> Path:
    """An empty folder to run agy in. agy treats its working folder as the workspace, so running it
    in the server's folder (often the user's project) would let it read, and in accept-edits mode
    edit, files there. Only the folders passed with --add-dir should be reachable."""
    p = home() / "agy-cwd"
    p.mkdir(parents=True, exist_ok=True)
    return p


def agy_backend() -> str:
    """"acp" (default): one long-running `agy acp`; "cli": a fresh `agy -p` per call."""
    b = acp.env_backend()
    if b not in ("acp", "cli"):
        raise Fail(f"GEMINI_BUDDY_AGY_BACKEND must be acp or cli, got {b!r}")
    return b


def acp_client() -> acp.AcpClient:
    return acp.shared(lambda: acp.AcpClient(agy_exe(), agy_workspace(), agy_env(),
                                            acp.default_log_path(home())))


def run_agy(prompt: str, model: str, dirs: list[Path], mode: str = "plan",
            guard: str = GUARD, need_text: bool = True) -> dict:
    """Run one agy prompt. mode "plan" = read-only (consult / media analysis).

    Other modes (accept-edits, for agy's image generator) always use `agy -p`: outside plan mode
    ACP agy runs commands its settings allow (e.g. proceed-in-sandbox) without asking us, and ACP
    has no --settings to deny them. Over ACP agy hands images to a subagent, which starts without
    asking. (agy 1.5.14 reported the image after the turn ended; 1.5.16 keeps the turn open until
    the subagent is done, checked 2026-11-24.)"""
    if agy_backend() == "cli" or mode != "plan":
        return run_agy_cli(prompt, model, dirs, mode, guard, need_text)
    res = acp_client().prompt(guard + prompt, model, agy_workspace(), dirs, AGY_TIMEOUT)
    if need_text and not res["response"]:
        denied = res["denied_actions"]
        hint = " agy tried something only a person may approve (often a shell command or an edit): " \
               "do that step yourself (e.g. media_clip / media_frames) and ask agy only to look." if denied else ""
        raise Fail(f"agy returned no answer (stop {res['stop_reason']}, denied {denied}).{hint}")
    return res


def run_agy_cli(prompt: str, model: str, dirs: list[Path], mode: str = "plan",
                guard: str = GUARD, need_text: bool = True) -> dict:
    if mode != "plan":
        # A mode that may write runs in a throwaway folder, so nothing it leaves there (say a
        # rules file) is seen by later sessions in the shared agy-cwd.
        home().mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="agy-once-", dir=home()) as d:
            return _run_agy_cli(prompt, model, dirs, mode, guard, need_text, Path(d))
    return _run_agy_cli(prompt, model, dirs, mode, guard, need_text, agy_workspace())


def _run_agy_cli(prompt: str, model: str, dirs: list[Path], mode: str, guard: str, need_text: bool,
                 cwd: Path) -> dict:
    t0 = time.time()
    try:
        r = subprocess.run(agy_command(guard + prompt, model, dirs, mode), capture_output=True,
                           env=agy_env(), stdin=subprocess.DEVNULL, timeout=AGY_TIMEOUT + 60,
                           cwd=str(cwd))
    except FileNotFoundError:
        raise Fail(f"agy not found ({AGY}); install Antigravity CLI or set GEMINI_BUDDY_AGY")
    except subprocess.TimeoutExpired:
        raise Fail(f"agy did not finish within {AGY_TIMEOUT + 60} s")
    stdout, stderr = decode(r.stdout), decode(r.stderr)
    res = parse_json_tail(stdout)
    if res is None:
        raise Fail(f"agy failed (exit {r.returncode}): {clip(stderr or stdout, 1500)}")
    response = (res.get("response") or "").strip() if isinstance(res.get("response"), str) else ""
    if need_text and not response:
        denied = res.get("denied_actions") or []
        hint = " agy tried a tool headless mode cannot approve (often a shell command): do that step " \
               "yourself (e.g. media_clip / media_frames) and ask agy only to look." if denied else ""
        raise Fail(f"agy returned no answer (status {res.get('status')}, denied {denied}).{hint} "
                   f"{clip(stderr, 800)}")
    return {
        "response": response,
        "conversation_id": res.get("conversation_id"),
        "seconds": round(res.get("duration_seconds") or (time.time() - t0), 1),
        "denied_actions": res.get("denied_actions") or [],
        "usage": res.get("usage"),
        "started": t0,
    }


def resolve_existing(path: str) -> Path:
    p = Path(path).expanduser().resolve()
    if not p.exists():
        raise Fail(f"file not found: {path}")
    return p


def brain_root() -> Path:
    env = os.environ.get("AGY_BRAIN_DIR")
    return Path(env) if env else Path.home() / ".gemini" / "antigravity-cli" / "brain"


def agy_images(conv_id: str, since: float) -> list[Path]:
    """Images agy saved in its own brain/<conversation_id> folder during this call (newest first)."""
    folder = brain_root() / conv_id
    if not conv_id or not folder.is_dir():
        return []
    found = [p for p in folder.rglob("*")
             if p.is_file() and ".system_generated" not in p.parts
             and p.suffix.lower() in (".png", ".jpg", ".jpeg", ".webp") and p.stat().st_mtime >= since - 5]
    return sorted(found, key=lambda p: p.stat().st_mtime, reverse=True)


# ---------------------------------------------------------------- genai backend

def genai_client(api_version: str | None = None):
    """api_version: the SDK may default to the stable v1 API, which lacks some fields
    (TTS's speech_config is only in v1beta)."""
    key = (os.environ.get("GEMINI_API_KEY") or "").strip()
    if not key:
        why = ("is set but empty (registered with `-e GEMINI_API_KEY=...` from a shell that had no key? "
               "Register without -e so the server inherits it, then restart the client)"
               if "GEMINI_API_KEY" in os.environ else "is not set")
        raise Fail(f"GEMINI_API_KEY {why}; API generation uses the API credit "
                   "(for images, backend='agy' uses the subscription instead)")
    from google import genai
    if api_version:
        from google.genai import types
        return genai.Client(api_key=key, http_options=types.HttpOptions(api_version=api_version))
    return genai.Client(api_key=key)


def usage_of(resp) -> dict[str, Any] | None:
    u = getattr(resp, "usage_metadata", None)
    if u is None:
        return None
    keys = ("prompt_token_count", "candidates_token_count", "total_token_count")
    return {k: getattr(u, k, None) for k in keys if getattr(u, k, None) is not None}


def model_hint(client, word: str) -> str:
    """On a bad model id, list the ids that look right so the caller can fix the env var."""
    try:
        names = [m.name.removeprefix("models/") for m in client.models.list() if word in m.name]
        return f" Models with '{word}' in the name: {', '.join(names[:15]) or 'none'}."
    except Exception:  # listing is only a hint
        return ""


def hint_if_missing(e: Exception, client, word: str) -> str:
    """model_hint, but only when the error says the model was not found (not for a refused prompt)."""
    text = str(e).lower()
    return model_hint(client, word) if ("not found" in text or "not_found" in text or "404" in text) else ""


def output_file(path: str) -> Path:
    p = Path(path).expanduser().resolve()
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def free_path(p: Path) -> Path:
    """`p`, or `name-1.ext`, `name-2.ext`... when something is already there."""
    if not p.exists():
        return p
    n = 1
    while (q := p.with_name(f"{p.stem}-{n}{p.suffix}")).exists():
        n += 1
    return q


MIME_EXT = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp"}
EXT_MIME = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp"}


def write_image(data: bytes, mime: str, out: Path) -> Path:
    """Save as `out`; convert with Pillow if the extension differs from what the model sent."""
    want = out.suffix.lower()
    got = MIME_EXT.get(mime, ".png")
    if want == got or (want == ".jpeg" and got == ".jpg"):
        out.write_bytes(data)
        return out
    try:
        import io
        from PIL import Image
        img = Image.open(io.BytesIO(data))
        if want in (".jpg", ".jpeg") and img.mode != "RGB":
            img = img.convert("RGB")
        img.save(out)
        return out
    except ImportError:
        alt = free_path(out.with_suffix(got))  # never overwrite a different file the user has
        alt.write_bytes(data)
        return alt


def pcm_rate(mime: str) -> int:
    for part in (mime or "").split(";"):
        k, _, v = part.strip().partition("=")
        if k == "rate" and v.isdigit():
            return int(v)
    return 24000


def write_wav(pcm: bytes, rate: int, path: Path) -> None:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm)


def write_audio(pcm: bytes, rate: int, out: Path) -> Path:
    """TTS returns 16-bit mono PCM. Write WAV; other extensions go through ffmpeg if present.

    The intermediate WAV is a temporary file, so an existing `<name>.wav` next to `out` is never
    touched. Without ffmpeg (or if it fails) the WAV is kept under a free `<name>.wav` name.
    """
    if out.suffix.lower() == ".wav":
        write_wav(pcm, rate, out)
        return out
    return convert_or_keep(lambda tmp: write_wav(pcm, rate, tmp), ".wav", out)


def convert_or_keep(write, got_ext: str, out: Path) -> Path:
    """`write(tmp)` saves the media as a temporary `got_ext` file next to `out`; ffmpeg converts it
    to `out`. Without ffmpeg (or if it fails) it is kept under a free `<name><got_ext>` name, so an
    existing file of that name is never touched."""
    fd, tmp_name = tempfile.mkstemp(dir=out.parent, prefix=f".{out.stem}-", suffix=got_ext)
    os.close(fd)
    tmp = Path(tmp_name)
    try:
        write(tmp)
        try:
            ffmpeg = media.find_ffmpeg()
            r = subprocess.run([ffmpeg, "-y", "-loglevel", "error", "-i", str(tmp), str(out)],
                               capture_output=True, stdin=subprocess.DEVNULL)
            if r.returncode == 0:
                return out
        except Fail:
            pass
        kept = free_path(out.with_suffix(got_ext))
        os.replace(tmp, kept)
        return kept
    finally:
        tmp.unlink(missing_ok=True)


def write_media(data: bytes, got_ext: str, out: Path) -> Path:
    """Save generated audio / video bytes as `out`, converting with ffmpeg when the extension differs."""
    want = out.suffix.lower()
    if not want or want == got_ext:
        out = out if want else out.with_suffix(got_ext)
        out.write_bytes(data)
        return out
    return convert_or_keep(lambda tmp: tmp.write_bytes(data), got_ext, out)


# ---------------------------------------------------------------- tools: Gemini

mcp = FastMCP("gemini-buddy-mcp", instructions=INSTRUCTIONS)


def with_denied(res: dict) -> dict:
    return {"denied_actions": res["denied_actions"]} if res["denied_actions"] else {}


@mcp.tool()
async def gemini_consult(prompt: str, file_paths: list[str] | None = None,
                         session_id: str | None = None, model: str | None = None) -> dict:
    """Ask Gemini (via the agy CLI, subscription) for a second opinion on code or a design.

    file_paths: files Gemini should read (it opens them itself; nothing is uploaded here).
    session_id: continue an earlier session; omit to start a new one (the id is returned).
    model: agy model id (default gemini-4-neon-high; see `agy models`).
    For reviewing a git diff, gemini_review is better. Returns Gemini's answer as text.
    """
    s = open_session(session_id, prompt)
    files = [resolve_existing(f) for f in (file_paths or [])]
    body = prompt
    if files:
        body += "\n\nRead these files before answering (they are the subject of the question):\n"
        body += "\n".join(f"- {f}" for f in files)
    use = model or CONSULT_MODEL
    res = await anyio.to_thread.run_sync(
        run_agy, session_context(s) + body, use, [f if f.is_dir() else f.parent for f in files])
    turn = Turn(tool="gemini_consult", at=now(), prompt=prompt, response=res["response"],
                files=[str(f) for f in files], model=use, agy_conversation_id=res["conversation_id"])
    update_session(s, lambda x: x.turns.append(turn))
    return {"session_id": s.session_id, "model": use, "seconds": res["seconds"],
            "response": clip(res["response"], RESPONSE_CHARS), **with_denied(res)}


@mcp.tool()
async def gemini_analyze_media(file_path: str, prompt: str, session_id: str | None = None,
                               model: str | None = None, more_files: list[str] | None = None) -> dict:
    """Have Gemini (via the agy CLI, subscription) watch / listen to / look at media files.

    Transcription and speaker separation, what happens on screen, whether BGM suits a scene,
    rough positions in a frame. agy cannot run commands: cut long media first with media_clip /
    media_audio / media_frames (30-60 s, 720p). more_files: extra files for the same question
    (e.g. a few frames; keep it under ~10 images). For coordinates, use frames with a grid and
    model gemini-4-neon-high. The answer is summarised into the session.
    model: agy model id (default gemini-4-hydrogen-high).
    """
    s = open_session(session_id, prompt)
    files = [resolve_existing(file_path)] + [resolve_existing(f) for f in (more_files or [])]
    listing = "\n".join(f"- {f}" for f in files)
    body = (f"Media file{'s' if len(files) > 1 else ''}:\n{listing}\n"
            f"Open {'each' if len(files) > 1 else 'it'} once with your file-reading tool, then do this:\n\n{prompt}")
    use = model or MEDIA_MODEL
    res = await anyio.to_thread.run_sync(run_agy, session_context(s) + body, use, [f.parent for f in files])
    turn = Turn(tool="gemini_analyze_media", at=now(), prompt=prompt, response=res["response"],
                files=[str(f) for f in files], model=use, agy_conversation_id=res["conversation_id"])
    note = MediaNote(file=str(files[0]), more_files=[str(f) for f in files[1:]], prompt=prompt,
                     summary=clip(res["response"], SUMMARY_CHARS), at=now())

    def change(x: Session) -> None:
        x.turns.append(turn)
        x.media.append(note)

    update_session(s, change)
    return {"session_id": s.session_id, "model": use, "seconds": res["seconds"],
            "files": [str(f) for f in files], "response": clip(res["response"], RESPONSE_CHARS),
            **with_denied(res)}


INLINE_IMAGES_BYTES = 18 * 1024 * 1024   # generate_content's inline request cap is 20 MB


def image_parts(paths: list[Path]) -> list:
    """Input images as inline parts (they come before the prompt)."""
    from google.genai import types
    parts, total = [], 0
    for p in paths:
        kind, mime = interact.kind_of(p)
        if kind != "image":
            raise Fail(f"input_paths takes images only, not {p.name}")
        data = p.read_bytes()
        total += len(data)
        parts.append(types.Part.from_bytes(data=data, mime_type=mime))
    if total > INLINE_IMAGES_BYTES:
        raise Fail(f"input images are {total // 1048576} MB together; keep them under 18 MB "
                   "(shrink or convert them to JPEG first)")
    return parts


def _generate_image_api(prompt: str, out: Path, context: str, inputs: list[Path] | None = None,
                        aspect_ratio: str | None = None,
                        image_size: str | None = None) -> tuple[Path, str, dict | None, str]:
    from google.genai import types
    client = genai_client()
    parts = image_parts(inputs or [])
    look = ({k: v for k, v in (("aspect_ratio", aspect_ratio), ("image_size", image_size)) if v})
    try:
        resp = client.models.generate_content(
            model=IMAGE_MODEL, contents=parts + [context + prompt] if parts else context + prompt,
            config=types.GenerateContentConfig(
                response_modalities=["IMAGE", "TEXT"],
                image_config=types.ImageConfig(**look) if look else None))
    except Exception as e:
        raise Fail(f"image generation failed with {IMAGE_MODEL}: {e}.{model_hint(client, 'image')}")
    image, texts = None, []
    for cand in resp.candidates or []:
        for part in (cand.content.parts if cand.content else None) or []:
            if part.inline_data and part.inline_data.data and image is None:
                image = part.inline_data
            elif part.text:
                texts.append(part.text)
    if image is None:
        raise Fail(f"the model returned no image: {clip(' '.join(texts), 800) or 'no text either'}")
    saved = write_image(image.data, image.mime_type or "image/png", out)
    return saved, " ".join(texts), usage_of(resp), IMAGE_MODEL


def _generate_image_agy(prompt: str, out: Path, context: str) -> tuple[Path, str, dict | None, str]:
    # agy's image generator saves into its brain folder; accept-edits lets it write there
    # (agy-media used the same mode). The guard forbids touching any other file.
    res = run_agy(context + f"Generate this image with your image generation tool:\n{prompt}",
                  AGY_IMAGE_MODEL, [], mode="accept-edits", guard=IMAGE_GUARD, need_text=False)
    found = agy_images(res["conversation_id"] or "", res["started"])
    if not found:
        raise Fail(f"agy made no image (answer: {clip(res['response'], 500) or 'none'}, "
                   f"denied {res['denied_actions']})")
    src = found[0]
    saved = write_image(src.read_bytes(), EXT_MIME.get(src.suffix.lower(), "image/png"), out)
    return saved, res["response"], res["usage"], f"agy:{AGY_IMAGE_MODEL}"


@mcp.tool()
async def gemini_generate_image(prompt: str, output_path: str, session_id: str | None = None,
                                backend: Literal["api", "agy"] = "api",
                                input_paths: list[str] | None = None,
                                aspect_ratio: str | None = None,
                                image_size: Literal["512", "1K", "2K", "4K"] | None = None) -> dict:
    """Generate or edit an image and save it to output_path. Returns the saved path, not the image.

    backend: "api" = google-genai SDK with GEMINI_API_KEY (API credit, fast);
             "agy" = agy's built-in generator (subscription, ~50 s per image).
    input_paths (api only): images the model sees before the prompt. One image + "make the sky
    red" edits it; several are references ("this character in this room"; up to 14). To edit an
    image made earlier, pass its path (the session's `history`).
    aspect_ratio: "1:1", "3:2", "2:3", "4:3", "3:4", "4:5", "5:4", "16:9", "9:16", "21:9" ...;
    image_size: "1K" (default), "2K", "4K" ("512" only on some models). Both api only.
    With a session_id, earlier images and analyses in that session are given as context (as
    text: the model does not see earlier images unless they are in input_paths).
    Read the saved image to check it.
    """
    s = open_session(session_id, prompt)
    out = output_file(output_path)
    inputs = [resolve_existing(p) for p in (input_paths or [])]
    if backend == "agy" and (inputs or aspect_ratio or image_size):
        raise Fail("input_paths, aspect_ratio and image_size need backend='api'")
    ctx = ""
    if any(a.kind == "image" for a in s.artifacts) or s.media:
        ctx = session_context(s).replace("## New request\n", "## Image to make now\n")
    if backend == "agy":
        saved, note, usage, used = await anyio.to_thread.run_sync(_generate_image_agy, prompt, out, ctx)
    else:
        saved, note, usage, used = await anyio.to_thread.run_sync(
            lambda: _generate_image_api(prompt, out, ctx, inputs, aspect_ratio, image_size))
    art = Artifact(kind="image", path=str(saved), prompt=prompt, model=used, at=now(), usage=usage)
    turn = Turn(tool="gemini_generate_image", at=now(), prompt=prompt, response=note,
                files=[str(f) for f in inputs] + [str(saved)], model=used)

    def change(x: Session) -> None:
        x.artifacts.append(art)
        x.turns.append(turn)

    latest = update_session(s, change)
    return {"session_id": s.session_id, "path": str(saved), "model": used, "backend": backend,
            "model_note": clip(note, 1000), "usage": usage,
            "history": [a.path for a in latest.artifacts if a.kind == "image"]}


def _generate_audio(text: str, voice: str, out: Path) -> tuple[Path, float, dict | None]:
    from google.genai import types
    client = genai_client("v1beta")
    try:
        resp = client.models.generate_content(
            model=TTS_MODEL, contents=text,
            config=types.GenerateContentConfig(
                response_modalities=["AUDIO"],
                speech_config=types.SpeechConfig(voice_config=types.VoiceConfig(
                    prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=voice)))))
    except Exception as e:
        raise Fail(f"speech generation failed with {TTS_MODEL}: {e}.{model_hint(client, 'tts')}")
    chunks, mime = [], ""
    for cand in resp.candidates or []:
        for part in (cand.content.parts if cand.content else None) or []:
            if part.inline_data and part.inline_data.data:
                chunks.append(part.inline_data.data)
                mime = mime or (part.inline_data.mime_type or "")
    if not chunks:
        raise Fail("the model returned no audio")
    pcm, rate = b"".join(chunks), pcm_rate(mime)
    return write_audio(pcm, rate, out), round(len(pcm) / 2 / rate, 2), usage_of(resp)


@mcp.tool()
async def gemini_generate_audio(text: str, output_path: str, voice_name: str = "Kore",
                                session_id: str | None = None) -> dict:
    """Read text aloud with Gemini TTS (google-genai SDK, API credit) and save it to output_path.

    .wav is written directly; other extensions (.mp3, .m4a ...) are converted with ffmpeg when
    available, otherwise a .wav next to it is returned. Put only the words to be spoken in text:
    gemini-3.8-flash-tts reads directions such as "Say cheerfully:" aloud. Choose the tone with
    voice_name instead. Returns the saved path and the length in seconds; to check what was said,
    pass the file to gemini_analyze_media.
    """
    s = open_session(session_id, text)
    out = output_file(output_path)
    saved, seconds, usage = await anyio.to_thread.run_sync(_generate_audio, text, voice_name, out)
    art = Artifact(kind="audio", path=str(saved), prompt=text, model=TTS_MODEL, at=now(), usage=usage)
    turn = Turn(tool="gemini_generate_audio", at=now(), prompt=text,
                response=f"voice {voice_name}, {seconds} s", files=[str(saved)], model=TTS_MODEL)

    def change(x: Session) -> None:
        x.artifacts.append(art)
        x.turns.append(turn)

    update_session(s, change)
    return {"session_id": s.session_id, "path": str(saved), "seconds": seconds,
            "voice_name": voice_name, "model": TTS_MODEL, "usage": usage}


# ---------------------------------------------------------------- music / video (Interactions API)

def record_made(s: Session, kind: str, tool: str, prompt: str, saved: Path, model: str,
                note: str, usage: dict | None, inputs: list[Path] = ()) -> Session:
    art = Artifact(kind=kind, path=str(saved), prompt=prompt, model=model, at=now(), usage=usage)
    turn = Turn(tool=tool, at=now(), prompt=prompt, response=note,
                files=[str(f) for f in inputs] + [str(saved)], model=model)

    def change(x: Session) -> None:
        x.artifacts.append(art)
        x.turns.append(turn)

    return update_session(s, change)


def _generate_music(prompt: str, out: Path, model: str) -> tuple[Path, str, dict | None]:
    client = genai_client("v1beta")
    extra: dict[str, Any] = {}
    if "clip" not in model:  # the 30 s clip model takes no response_format
        extra["response_format"] = {"type": "audio"}
        if out.suffix.lower() == ".wav":
            extra["response_format"]["mime_type"] = "audio/wav"
    try:
        it = client.interactions.create(model=model, input=prompt, timeout=600, **extra)
    except Exception as e:
        raise Fail(f"music generation failed with {model}: {e}.{hint_if_missing(e, client, 'lyria')}")
    if it.status in interact.FAILED or it.output_audio is None:
        raise Fail(f"no music came back (status {it.status}): {clip(interact.errors_text(it), 800) or 'no reason given'}")
    data, mime = interact.content_bytes(client, it.output_audio)
    saved = write_media(data, interact.out_ext(mime, ".mp3"), out)
    return saved, it.output_text or "", interact.usage(it)


@mcp.tool()
async def gemini_generate_music(prompt: str, output_path: str, model: str | None = None,
                                session_id: str | None = None) -> dict:
    """Make music with Lyria (Interactions API, API credit, about $0.08 a song) and save it.

    Describe genre, mood, tempo (BPM), instruments and structure; set the length in the prompt
    ("a 90-second loop", or timed sections "[0:00-0:15] intro: soft piano ..."). The length is
    only a target (asked for 30 s, got 64 s): check `seconds` and trim if it must be exact.
    Vocals: give lyrics with [Verse] / [Chorus] tags; for BGM say "Instrumental only, no vocals."
    Prompts naming artists or quoting existing lyrics are refused, and so are prompts too close
    to existing music ("recitation"): describe it as an original track with concrete details.
    model: default lyria-3.5 (full songs, up to a few minutes); lyria-3-clip-preview is always 30 s.
    The model sends MP3 (lyria-3.5 can send WAV, asked for when output_path ends in .wav);
    other extensions are converted with ffmpeg. Returns the path, the length, and the model's
    notes (lyrics / structure). Claude can't hear it: check with gemini_analyze_media or the user.
    """
    s = open_session(session_id, prompt)
    out = output_file(output_path)
    use = model or MUSIC_MODEL
    saved, notes, usage = await anyio.to_thread.run_sync(_generate_music, prompt, out, use)
    seconds = await anyio.to_thread.run_sync(media.duration, saved)
    record_made(s, "music", "gemini_generate_music", prompt, saved, use,
                f"{seconds} s. {notes}".strip(), usage)
    return {"session_id": s.session_id, "path": str(saved), "seconds": seconds, "model": use,
            "notes": clip(notes, 4000), "usage": usage}


def videos_dir() -> Path:
    return home() / "videos"


def video_job_path(interaction_id: str) -> Path:
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in interaction_id)
    return videos_dir() / f"{safe}.json"


def save_video_job(job: dict) -> None:
    p = video_job_path(job["id"])
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=p.parent, suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(json.dumps(job, ensure_ascii=False, indent=2))
    os.replace(tmp, p)


def load_video_job(interaction_id: str) -> dict:
    p = video_job_path(interaction_id)
    if not p.exists():
        raise Fail(f"no video job {interaction_id} (started by gemini_generate_video on this machine?)")
    return json.loads(p.read_text(encoding="utf-8"))


VIDEO_WAIT_MAX = 110.0   # one tool call waits at most this long; then call gemini_video_status again


def _start_video(prompt: str, inputs: list[Path], previous: str | None, aspect_ratio: str,
                 resolution: str, duration_seconds: int | None, task: str | None, model: str) -> str:
    client = genai_client("v1beta")
    fmt: dict[str, Any] = {"type": "video", "aspect_ratio": aspect_ratio, "resolution": resolution}
    if duration_seconds is not None:
        fmt["duration"] = f"{duration_seconds}s"
    extra: dict[str, Any] = {}
    if previous:
        extra["previous_interaction_id"] = previous
    if task:
        extra["generation_config"] = {"video_config": {"task": task}}
    body = interact.input_blocks(client, inputs, prompt)
    try:
        it = client.interactions.create(model=model, input=body, response_format=fmt,
                                        background=True, timeout=120, **extra)
    except Exception as e:
        raise Fail(f"video generation failed to start with {model}: {e}.{hint_if_missing(e, client, 'omni')}")
    if not it.id:
        raise Fail(f"the API returned no interaction id (status {it.status})")
    return it.id


def _poll_video(job: dict, wait_seconds: float) -> dict:
    """Check the job (waiting up to wait_seconds); save the video when it is done. Updates `job`."""
    client = genai_client("v1beta")
    deadline = time.time() + max(0.0, min(wait_seconds, VIDEO_WAIT_MAX))
    delay = 5.0
    while True:
        try:
            it = client.interactions.get(job["id"], timeout=60)
        except Exception as e:
            raise Fail(f"could not read video job {job['id']}: {e}")
        status = str(it.status)
        if status == interact.DONE:
            if it.output_video is None:
                job.update(status="failed", error=f"finished without a video: {clip(interact.errors_text(it), 800)}")
                return job
            data, mime = interact.content_bytes(client, it.output_video)
            saved = write_media(data, interact.out_ext(mime, ".mp4"), Path(job["output_path"]))
            job.update(status="done", path=str(saved), usage=interact.usage(it),
                       notes=it.output_text or "", finished=now())
            return job
        if status in interact.FAILED or status == "requires_action":
            job.update(status=status, error=clip(interact.errors_text(it), 2000) or "no reason given")
            return job
        job["status"] = "running"
        left = deadline - time.time()
        if left <= 0:
            return job
        time.sleep(min(delay, left))
        delay = min(delay * 1.5, 20.0)


def video_answer(job: dict) -> dict:
    keys = ("id", "status", "path", "seconds", "model", "session_id", "error", "notes", "usage", "started")
    out = {k: job[k] for k in keys if job.get(k) not in (None, "")}
    out["interaction_id"] = out.pop("id")
    if job["status"] == "running":
        out["next"] = "call gemini_video_status with this interaction_id (wait_seconds up to 110)"
    elif job["status"] == "done":
        out["next"] = ("check it (media_frames / gemini_analyze_media); to edit or extend it, call "
                       "gemini_generate_video with previous_interaction_id set to this interaction_id")
    return out


def finish_video(job: dict) -> dict:
    """Save the job; when it just finished, measure it and note it in the session (once)."""
    if job["status"] == "done" and not job.get("recorded"):
        job["seconds"] = media.duration(Path(job["path"]))
        if job.get("session_id"):
            try:
                s = open_session(job["session_id"], job["prompt"])
                record_made(s, "video", "gemini_generate_video", job["prompt"], Path(job["path"]),
                            job["model"], f"{job.get('seconds')} s. {job.get('notes', '')}".strip(),
                            job.get("usage"), [Path(f) for f in job.get("inputs", [])])
            except Fail as e:
                log.warning("video %s: could not note it in the session: %s", job["id"], e)
        job["recorded"] = True
    save_video_job(job)
    return video_answer(job)


@mcp.tool()
async def gemini_generate_video(prompt: str, output_path: str, input_paths: list[str] | None = None,
                                previous_interaction_id: str | None = None,
                                aspect_ratio: Literal["16:9", "9:16"] = "16:9",
                                resolution: Literal["360p", "720p", "1080p", "4k"] = "720p",
                                duration_seconds: int | None = None,
                                task: Literal["text_to_video", "image_to_video", "reference_to_video",
                                              "edit", "extend"] | None = None,
                                model: str | None = None, wait_seconds: float = 100,
                                session_id: str | None = None) -> dict:
    """Make a video with Gemini Omni (Interactions API, API credit) and save it to output_path.

    Costs roughly $0.07 per second at 720p ($0.02 at 360p, $0.10 at 1080p, $0.21 at 4k), so
    try ideas at 360p. duration_seconds: 3-15 (model default 10). Generation runs in the
    background: this call waits up to wait_seconds (max 110); if it is not done yet it returns
    status "running" and an interaction_id for gemini_video_status.
    input_paths: images (first frame, or references for characters / objects; up to 6) and
    videos (up to 15 s) to edit; they go before the prompt. Images are sent inline, videos and
    audio are uploaded with the Files API (kept 48 h).
    previous_interaction_id: the interaction_id of a video made earlier, to edit it ("make it
    night") or extend it ("continue: the camera pans left"; +3-10 s per turn, 30 s in total).
    task: forces a mode; usually leave it out and say what you want in the prompt.
    model: default gemini-omni-1.2-flash. Claude can't watch the result: check it with
    media_frames / gemini_analyze_media.
    """
    s = open_session(session_id, prompt)
    if duration_seconds is not None and not 3 <= duration_seconds <= 15:
        raise Fail("duration_seconds must be 3-15")
    out = output_file(output_path)
    inputs = [resolve_existing(p) for p in (input_paths or [])]
    for p in inputs:
        interact.kind_of(p)  # fail on an unsupported file before anything is uploaded
    use = model or VIDEO_MODEL
    iid = await anyio.to_thread.run_sync(lambda: _start_video(
        prompt, inputs, previous_interaction_id, aspect_ratio, resolution, duration_seconds, task, use))
    job = {"id": iid, "status": "running", "prompt": prompt, "output_path": str(out), "model": use,
           "session_id": s.session_id, "inputs": [str(p) for p in inputs],
           "previous_interaction_id": previous_interaction_id, "started": now()}
    if not session_path(s.session_id).exists():
        save_session(s)  # so the finished video can be noted in it, even from a later call
    save_video_job(job)
    job = await anyio.to_thread.run_sync(_poll_video, job, wait_seconds)
    return await anyio.to_thread.run_sync(finish_video, job)


@mcp.tool()
async def gemini_video_status(interaction_id: str, wait_seconds: float = 100,
                              cancel: bool = False) -> dict:
    """Check a video started by gemini_generate_video; save it to its output_path when done.

    wait_seconds: wait up to this long (max 110) for it to finish. cancel=True stops a running
    job (what was spent so far may still be billed). A finished job just returns its path again.
    """
    job = load_video_job(interaction_id)
    if job["status"] == "done":
        return video_answer(job)
    if cancel:
        def stop() -> dict:
            try:
                genai_client("v1beta").interactions.cancel(job["id"], timeout=60)
            except Exception as e:
                raise Fail(f"cancel failed: {e}")
            job.update(status="cancelled", error="cancelled by request")
            return finish_video(job)
        return await anyio.to_thread.run_sync(stop)
    job = await anyio.to_thread.run_sync(_poll_video, job, wait_seconds)
    return await anyio.to_thread.run_sync(finish_video, job)


@mcp.tool()
async def gemini_review(repo: str, base: str | None = None, commit: str | None = None,
                        staged: bool = False, intent: str | None = None,
                        models: list[str] | None = None, allow_secret_like: bool = False,
                        session_id: str | None = None, out_dir: str | None = None) -> dict:
    """Independent code review of a git diff by Gemini (agy, subscription, read-only plan mode).

    What to review (pick one; default = uncommitted changes vs HEAD): base="origin/main" for this
    branch, commit="<sha>" for one commit, staged=True for staged changes.
    intent: what the change is meant to do, in the author's words (default: commit messages).
      Do not put your own review opinions here; the point is an independent second look.
    models: agy models (default gemini-4-neon-high alone; another Gemini adds little next to
      Claude). With several, findings on the same place are merged; `agreed` = how many models.
    Findings come back as a list; `unparsed` holds any answer whose findings could not be read
    as JSON: read it yourself.
    Secret-looking lines stop the run with status STOPPED and nothing is sent; show those lines to
    the user and pass allow_secret_like=True only if they say it is fine.
    Takes minutes. Findings are candidates: trace each one in the code before reporting it.
    """
    target = f"commit {commit}" if commit else f"branch vs {base}" if base else \
        "staged changes" if staged else "uncommitted changes"
    s = open_session(session_id, f"review {target} in {repo}")  # before minutes of work
    work = work_dir("review", out_dir, session_id)
    client = acp_client if agy_backend() == "acp" else None
    result = await anyio.to_thread.run_sync(lambda: review_mod.review(
        repo, base=base, commit=commit, staged=staged, intent=intent, models=models, out=work,
        allow_secret_like=allow_secret_like, client=client() if client else None))
    if result.get("status") == "STOPPED":
        return result
    lines = [f"- [{g['severity']}] {g['file']}:{g['line']} (agreed {g['agreed']}): "
             f"{g['items'][0].get('summary', '')}" for g in result["findings"]]
    used = ", ".join(m["model"] for m in result["models"])
    turn = Turn(tool="gemini_review", at=now(), prompt=f"review {target} in {repo}",
                response="\n".join(lines) or "no findings", files=[result["review_file"]], model=used)
    art = Artifact(kind="review", path=result["review_file"], prompt=target, at=now(), model=used)

    def change(x: Session) -> None:
        x.turns.append(turn)
        x.artifacts.append(art)

    update_session(s, change)
    return {"session_id": s.session_id, **result}


DELEGATE_MODEL = os.environ.get("GEMINI_BUDDY_DELEGATE_MODEL", "gemini-4-neon-high")


@mcp.tool()
async def gemini_delegate_start(task: str, repo: str, base: str | None = None, model: str | None = None,
                                allow_commands: list[str] | None = None,
                                session_id: str | None = None) -> dict:
    """Hand a coding task to Gemini (agy, subscription), which works on it alone in a new git worktree.

    Returns at once with a task_id; follow it with gemini_delegate_status. Nothing is merged: when
    agy is done, its changes are staged in the worktree and written to changes.patch. Read the
    diff yourself, then take in what is good (e.g. `git apply <patch_file>` in your checkout).
    repo: a folder in the git repository. base: commit / branch to start from (default HEAD;
      uncommitted changes in your checkout are NOT in the worktree, so commit them first if needed).
    task: what to do, self-contained (agy sees only the worktree and this text).
    allow_commands: commands agy may run, matched as a prefix (e.g. ["python -m unittest"]);
      none by default. Shell operators are refused. An allowed command can run code agy wrote.
    agy may read and edit only inside the worktree. Anything else is refused, and a refusal ends
    its turn (it is nudged to go on, at most twice). Subagents, or anything run without asking,
    stop the task. Before each task a short check confirms agy asks before acting (~10 s).
    model: agy model (default gemini-4-neon-high).
    """
    if session_id:
        require_session(session_id)
    use = model or DELEGATE_MODEL
    t = await anyio.to_thread.run_sync(lambda: delegate.start(
        home(), repo, task, use, base=base, allow_commands=allow_commands, session_id=session_id))
    if session_id:
        art = Artifact(kind="delegate", path=str(t.folder), prompt=task, model=use, at=now())
        turn = Turn(tool="gemini_delegate_start", at=now(), prompt=task,
                    response=f"task {t.id} in worktree {t.worktree}", model=use)

        def change(x: Session) -> None:
            x.turns.append(turn)
            x.artifacts.append(art)

        update_session(open_session(session_id, task), change)
    return {"task_id": t.id, "status": t.status, "worktree": str(t.worktree), "branch": t.branch,
            "base_sha": t.base_sha, "model": use, "allow_commands": t.commands}


@mcp.tool()
async def gemini_delegate_status(task_id: str, wait_seconds: float = 0, since: int = 0) -> dict:
    """Progress of a delegated task: tool calls (with what was allowed / denied) and agy's messages.

    status: starting / checking (isolation check) / running / done / failed / stopped (agy did
    something not allowed; see warnings) / cancelled / interrupted (the server restarted).
    When not running: response (agy's summary) and changes (files, stat, patch_file, patch).
    wait_seconds: wait up to this long (max 110) for the task to finish before answering.
    since: pass next_since from the last answer to get only events that are new or changed since
      (a tool call's status and permission are updated in place).
    Check the diff in the code yourself; agy's summary is a claim, not a test result.
    """
    return await anyio.to_thread.run_sync(lambda: delegate.status(home(), task_id, since, wait_seconds))


@mcp.tool()
async def gemini_delegate_reply(task_id: str, message: str) -> dict:
    """Send a follow-up to a finished task in the same agy session (e.g. review comments to fix).

    Works while agy is still up (GEMINI_BUDDY_DELEGATE_IDLE seconds after the last turn, default
    1800). Then follow it with gemini_delegate_status as after start.
    """
    return await anyio.to_thread.run_sync(lambda: delegate.reply(home(), task_id, message))


@mcp.tool()
async def gemini_delegate_cancel(task_id: str, discard: bool = False) -> dict:
    """Stop a delegated task (kills its agy, subagents included). The worktree is kept to inspect.

    discard: also remove the worktree and its branch (also for finished tasks, to clean up).
    """
    return await anyio.to_thread.run_sync(lambda: delegate.cancel(home(), task_id, discard))


@mcp.tool()
def gemini_list_sessions(limit: int = 20) -> dict:
    """List saved sessions, newest first: session_id, updated time, title and related files.

    Pass a session_id from here to any other tool to continue that session.
    """
    rows = []
    for p in sessions_dir().glob("*.json"):
        try:
            s = Session.model_validate_json(p.read_text(encoding="utf-8"))
        except Exception as e:  # keep listing even if one file is damaged
            log.warning("skipping %s: %s", p, e)
            continue
        rows.append({"session_id": s.session_id, "updated_at": s.updated_at, "title": s.title,
                     "turns": len(s.turns), "files": s.files()})
    rows.sort(key=lambda r: r["updated_at"], reverse=True)
    return {"count": len(rows), "sessions": rows[:max(1, limit)]}


# ---------------------------------------------------------------- tools: local prep / measurement

@mcp.tool()
async def media_clip(video_path: str, start: float, end: float, out_path: str | None = None,
                     height: int = 720, fps: float = 30, audio: bool = True,
                     session_id: str | None = None) -> dict:
    """Cut a small mp4 (720p, 30 fps by default) of a range, to hand to gemini_analyze_media.

    30-60 s clips work best (e.g. to check whether BGM suits a scene). Local ffmpeg; no model.
    """
    require_session(session_id)
    out = output_file(out_path) if out_path else work_dir("clip", None, session_id) / "clip.mp4"
    r = await anyio.to_thread.run_sync(lambda: media.cut_clip(video_path, out, start, end, height, fps, audio))
    record(session_id, "clip", r["path"], f"clip {start}-{end} s of {video_path}")
    return r


@mcp.tool()
async def media_frames(video_path: str, start: float = 0.0, end: float | None = None, fps: float = 2.0,
                       crop: str | None = None, grid: int = 0, sheet: bool = False,
                       out_dir: str | None = None, session_id: str | None = None) -> dict:
    """Still frames of a video range, with index.json (each frame's source time and crop offset).

    crop: "W:H:X:Y" in source pixels. grid: draw a labelled grid every N px, labelled in SOURCE
    video coordinates, so positions read off a frame need no conversion (use 100 for coordinates).
    sheet: also tile all frames into sheet.png. Read the frames yourself, or pass a few to
    gemini_analyze_media (more_files). Check positions afterwards with media_overlay.
    """
    require_session(session_id)
    out = work_dir("frames", out_dir, session_id)
    r = await anyio.to_thread.run_sync(lambda: media.frames(video_path, out, start, end, fps, crop, grid, sheet))
    record(session_id, "frames", r["index"], f"frames {start}-{end} s of {video_path}")
    return r


@mcp.tool()
async def media_audio(src_path: str, start: float = 0.0, end: float | None = None,
                      silence_db: float = -35.0, silence_min: float = 0.25,
                      out_dir: str | None = None, session_id: str | None = None) -> dict:
    """Audio prep and stats from wav / mp3 / a video's sound track (local ffmpeg; no model).

    Writes audio.wav (mono 16 kHz, small enough for gemini_analyze_media), wave.png, spectrum.png
    (both readable by you) and stats.json. Returns loudness (LUFS, true peak) and sound / silence
    spans in source seconds; silences match utterance breaks closely, so use these spans for exact
    times rather than the times a model reports.
    """
    require_session(session_id)
    out = work_dir("audio", out_dir, session_id)
    r = await anyio.to_thread.run_sync(
        lambda: media.audio(src_path, out, start, end, silence_db, silence_min))
    record(session_id, "audio_stats", r["stats"], f"audio stats {start}-{end} s of {src_path}")
    return r


@mcp.tool()
async def media_mix_balance(mix_path: str, voice_path: str, start: float = 0.0, end: float | None = None,
                            voice_start: float | None = None, win: float = 0.5, min_db: float = 6.0,
                            out_path: str | None = None, session_id: str | None = None) -> dict:
    """Measure how far the voice stands above the BGM in a finished mix, given the voice-only track.

    The voice track is aligned by cross-correlation and subtracted; the rest is BGM + SE. Returns
    the mean difference and buried_spans (windows where voice - BGM < min_db). Talk videos usually
    keep BGM 12-20 dB under the voice. Use this, not a model's ear, to judge BGM volume.
    Heavy EQ / reverb on the voice makes the BGM estimate too high. Per-window rows go to a JSON file.
    """
    require_session(session_id)
    out = output_file(out_path) if out_path else work_dir("balance", None, session_id) / "balance.json"
    r = await anyio.to_thread.run_sync(lambda: measure.mix_balance(
        mix_path, voice_path, start, end, voice_start, win, min_db, out))
    record(session_id, "mix_balance", str(out), f"mix balance {start}-{end} s of {mix_path}")
    return r


@mcp.tool()
async def media_track(video_path: str, start: float, end: float, box: str, out_path: str | None = None,
                      search: int = 10, every: int = 1, update: float = 0.7, hold: float = 0.45,
                      session_id: str | None = None) -> dict:
    """Follow one object through a video range with OpenCV template matching (no model).

    box: "x,y,w,h" of the object on the start frame, in source pixels (read it off a media_frames
    grid). Points whose match score is below `hold` keep the last position and are marked held.
    Good for UI, cursors, captions, small units; weak when effects cover the object (then take
    keyframes with gemini_analyze_media instead). Check the result with media_overlay.
    """
    require_session(session_id)
    out = output_file(out_path) if out_path else work_dir("track", None, session_id) / "track.json"
    r = await anyio.to_thread.run_sync(lambda: measure.track(
        video_path, start, end, box, out, search, every, update, hold))
    record(session_id, "track", r["path"], f"track {box} {start}-{end} s of {video_path}")
    return r


@mcp.tool()
async def media_overlay(index_path: str, track_paths: list[str] | None = None,
                        points: list[list[dict[str, Any]] | dict[str, Any]] | None = None,
                        out_path: str | None = None,
                        space: Literal["video", "image"] = "video", zoom: str | None = None,
                        cols: int = 4, width: int = 600, session_id: str | None = None) -> dict:
    """Draw positions on the frames from media_frames and tile them into one image to check by eye.

    index_path: the index.json from media_frames. Tracks come from track_paths (JSON files with a
    "track" list, e.g. from media_track) and/or points: each entry is one track, either a list of
    points or Gemini's answer as is ({"track": [...]}). A point is {"frame": "f_001.png", "x": ..,
    "y": ..} or {"t": seconds, "x": .., "y": ..}. Each track gets its own colour.
    space: "video" (source pixels) or "image" (cropped-frame pixels).
    zoom: "X:Y:W:H" region of each frame to keep. Read the returned image to judge the positions.
    """
    require_session(session_id)
    tracks: list = list(track_paths or []) + list(points or [])
    out = output_file(out_path) if out_path else work_dir("overlay", None, session_id) / "check.png"
    r = await anyio.to_thread.run_sync(lambda: media.overlay(index_path, tracks, out, space, zoom, cols, width))
    record(session_id, "overlay", r["path"], f"overlay on {index_path}")
    return r


def main() -> None:
    logging.basicConfig(level=logging.INFO, stream=sys.stderr)
    mcp.run()  # stdio


if __name__ == "__main__":
    main()
