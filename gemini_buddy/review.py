"""Independent code review of a git diff by agy (Gemini), ported from agy-media's agy-review.

With `paths`, whole files are reviewed instead of a change: their working-tree contents are sent
as a diff that adds every line, so the same secret check and findings format apply.

Gemini is trained apart from Claude, so its blind spots differ. agy reads the diff (and the
repository around it, read-only) and answers with findings as JSON. One model (Neon) is the
default: a second Gemini adds little to a Claude + Gemini pair. With several models, findings that
land on the same place are merged (`agreed`). The answers are candidates, not verdicts: check
each one in the code yourself.

Over ACP (the default) no schema is enforced: the prompt asks for the JSON and parse_answer
finds it in the answer; when it cannot, the raw answer is returned (`unparsed`) for a reader.
Over `agy -p` (GEMINI_BUDDY_AGY_BACKEND=cli) --json-schema enforces it.

- agy runs in plan mode (read-only), without GEMINI_API_KEY / GOOGLE_API_KEY in its environment,
  and is told not to use a terminal, the web, or files outside the repository. It can still read
  any file in the repository: keep secrets out of it with agy's own deny rules (settings.json).
- Files that usually hold secrets (.env, keys, credentials; any case) are left out of the diff,
  and nothing is sent if any line of the diff looks like a secret (unless allow_secret_like).
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

from .agyproc import agy_env, agy_exe, decode, parse_json_tail, strict_settings_arg
from .errors import Fail

# Neon found the most (incl. a real race the others missed) in agy-media's comparison.
DEFAULT_MODELS = ["gemini-4-neon-high"]

# left out of the diff sent to agy (case-insensitive: .ENV, Credentials.json, server.KEY ...)
SECRET_PATHS = [
    f":(exclude,glob,icase){p}" for p in (
        "**/.env", "**/.env.*", "**/*.pem", "**/*.key", "**/id_rsa*", "**/id_ed25519*",
        "**/*credentials*.json", "**/secrets.*", "**/*.secret",
    )
]
_NAME = r"[A-Za-z_][A-Za-z0-9_]*"
VAR_REF = (r"(?!(?:\$" + _NAME + r"|\$\{" + _NAME + r"\}|\$env:" + _NAME + r"|%" + _NAME + r"%)['\"])")
SECRET_LINE = re.compile(
    r"(AKIA[0-9A-Z]{16}|AIza[0-9A-Za-z_\-]{35}|ghp_[0-9A-Za-z]{36}|github_pat_[0-9A-Za-z_]{40,}"
    r"|sk-[A-Za-z0-9_\-]{20,}|xox[abprs]-[0-9A-Za-z\-]{10,}|-----BEGIN [A-Z ]*PRIVATE KEY-----"
    # api_key = "…" / "api_key": "…" / 'password': '…' / token: "…"
    # but not when the whole value is a reference to a variable ("$API_KEY", "${API_KEY}",
    # "$env:API_KEY", "%API_KEY%"), which holds no value itself. Values that only start with
    # $ or % ("$2b$12$..." hashes, "%2F..." encoded tokens) still count.
    r"|(?i:(api[_-]?key|secret|token|password)['\"]?\s*[:=]\s*['\"]" + VAR_REF + r"[^'\"\s]{12,}['\"]))"
)

SCHEMA = {
    "type": "object",
    "properties": {
        "findings": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "file": {"type": "string", "description": "repo-relative path"},
                    "line": {"type": "integer", "description": "line in the NEW version of the file"},
                    "severity": {"type": "string", "enum": ["high", "medium", "low"]},
                    "category": {"type": "string", "description": "e.g. correctness, concurrency, security, error-handling, performance"},
                    "summary": {"type": "string", "description": "one sentence, Japanese"},
                    "failure_scenario": {"type": "string", "description": "concrete input/state -> wrong result, Japanese"},
                    "confidence": {"type": "number", "description": "0..1"},
                },
                "required": ["file", "line", "severity", "category", "summary", "failure_scenario", "confidence"],
            },
        },
        "notes": {"type": "string", "description": "anything worth saying that is not a finding (Japanese, short)"},
    },
    "required": ["findings"],
}

GUARD = (
    "Rules for this session: you are a read-only code reviewer. You have NO terminal (any shell "
    "command is denied and ends your turn). Do not search the web. Do not create, edit or delete "
    "files, and do not write a plan. Read only files inside the repository folder and the review "
    "folder named below, and never open .env files, private keys or credential files. "
    "When done, answer directly with the JSON.\n\n"
)

TASK = """Task: review a code change in the repository at {repo}.
The diff is in {diff} (unified diff, paths relative to the repository root).
{intent}
How to work:
1. Read the diff. Then open the changed files (and the code they call or that calls them) inside the repository to understand the context. Do not stop at the diff.
2. Look for real defects introduced or exposed by this change: wrong logic, off-by-one, wrong conditions, missing cases, broken error handling, race conditions, resource leaks, security problems, API misuse, mismatches with the code that calls it, and behaviour that contradicts the stated intent.
3. Before reporting a finding, re-read the code and convince yourself it really fails. Give a concrete failure scenario (input or state -> wrong result). If you cannot, drop it or give it low confidence.
4. Do NOT report style, naming, formatting, missing comments or tests, or matters of taste. Do not report pre-existing problems in code the diff does not touch, unless the change makes them worse.
5. Fewer, real findings are better than many weak ones. An empty list is a valid answer.
Answer with JSON matching the schema: findings[] with file (repo-relative), line (in the new version of the file), severity (high/medium/low), category, summary and failure_scenario (write these two in Japanese), confidence (0..1); and optional notes (Japanese).
"""

TASK_FILES = """Task: review existing code in the repository at {repo}.
The files to review are in {diff}, written as a diff that adds every line (paths relative to the repository root; line numbers are those of the file). This is the code as it is now, not a change.
{intent}
How to work:
1. Read the files in {diff}. Then open the code they call or that calls them inside the repository to understand the context.
2. Look for real defects in these files: wrong logic, off-by-one, wrong conditions, missing cases, broken error handling, race conditions, resource leaks, security problems, API misuse, mismatches with the code that calls it, and behaviour that contradicts the stated intent. For code that computes numbers, also check the calculation itself: wrong formulas, mixed-up units or scales, wrong weights or denominators, integer division, rounding or float precision that changes the result, rows or columns read from the wrong place or silently dropped, and missing or zero values handled in a way that skews the result.
3. Before reporting a finding, re-read the code and convince yourself it really fails. Give a concrete failure scenario (input or state -> wrong result). If you cannot, drop it or give it low confidence.
4. Do NOT report style, naming, formatting, missing comments or tests, or matters of taste. Report only problems located in the listed files (the other code is context).
5. Fewer, real findings are better than many weak ones. An empty list is a valid answer.
Answer with JSON matching the schema: findings[] with file (repo-relative), line (line number in the file), severity (high/medium/low), category, summary and failure_scenario (write these two in Japanese), confidence (0..1); and optional notes (Japanese).
"""


FENCE = re.compile(r"```(?:json)?\s*\n(.*?)\n\s*```", re.S)


def parse_answer(text):
    """The model's answer when structured output is missing: find the object that holds `findings`.

    The answer may be plain JSON, JSON in a ```json fence, or JSON after some prose. Never take an
    inner object (a single finding) for the whole answer.
    """
    if not isinstance(text, str) or not text.strip():
        return None
    candidates = [text.strip(), *(m.group(1) for m in FENCE.finditer(text))]
    for c in candidates:
        try:
            obj = json.loads(c)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict) and "findings" in obj:
            return obj
    dec = json.JSONDecoder()
    for m in re.finditer(r"\{", text):
        try:
            obj, _ = dec.raw_decode(text, m.start())
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict) and "findings" in obj:
            return obj
    return None


def clean_findings(raw):
    """Keep only well-formed findings; agy may send null or odd types."""
    if not isinstance(raw, list):
        return []
    out = []
    for f in raw:
        if not isinstance(f, dict) or not f.get("file"):
            continue
        try:
            line = int(f.get("line") or 0)
        except (TypeError, ValueError):
            line = 0
        try:
            conf = float(f.get("confidence") or 0)
        except (TypeError, ValueError):
            conf = 0.0
        out.append(dict(f, line=line, confidence=conf))
    return out


def git(repo: Path, *args) -> str:
    r = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, stdin=subprocess.DEVNULL)
    if r.returncode != 0:
        raise Fail(f"git {' '.join(args)} failed: {decode(r.stderr).strip()}")
    return decode(r.stdout)


def build_diff(repo: Path, a) -> tuple[str, str]:
    """Return (diff text, default intent)."""
    common = ["--no-color", "--no-ext-diff", "-U8"]
    intent = ""
    if a.commit:
        parents = git(repo, "rev-list", "--parents", "-n", "1", a.commit).split()[1:]
        if len(parents) > 1:
            # a merge commit: review what it brought into the first parent (git show would give a
            # combined diff, empty for a clean merge)
            diff = git(repo, "diff", *common, f"{a.commit}^1", a.commit, "--", ".", *SECRET_PATHS)
        else:
            diff = git(repo, "show", "--format=", *common, a.commit, "--", ".", *SECRET_PATHS)
        intent = git(repo, "log", "-1", "--format=%s%n%n%b", a.commit).strip()
    elif a.base:
        mb = git(repo, "merge-base", a.base, "HEAD").strip()
        diff = git(repo, "diff", *common, mb, "--", ".", *SECRET_PATHS)
        intent = git(repo, "log", "--format=- %s", f"{mb}..HEAD").strip()
    elif a.staged:
        diff = git(repo, "diff", "--cached", *common, "--", ".", *SECRET_PATHS)
    else:
        diff = git(repo, "diff", "HEAD", *common, "--", ".", *SECRET_PATHS)
    return diff, intent


def pathspecs(root: Path, base_dir: Path, paths: list[str]) -> list[str]:
    """Repo-relative pathspecs for `paths` (absolute, or relative to base_dir; globs allowed)."""
    out = []
    for raw in paths:
        if not isinstance(raw, str) or not raw.strip():
            raise Fail("paths: give files or folders as non-empty strings")
        p = Path(raw.strip()).expanduser()
        p = Path(os.path.abspath(p if p.is_absolute() else base_dir / p))
        try:
            rel = p.relative_to(root)
        except ValueError:
            # resolve() follows a symlinked or subst'ed drive to the same place as root
            try:
                rel = p.resolve().relative_to(root)
            except ValueError:
                raise Fail(f"paths: {raw} is outside the repository {root}") from None
        out.append(rel.as_posix() if rel.parts else ".")
    return out


def build_files(root: Path, base_dir: Path, paths: list[str]) -> tuple[str, list[str]]:
    """Whole files under `paths` (tracked, or untracked and not ignored; working-tree contents) as
    a diff that adds every line. Secret-holding files, binaries and submodules are left out.

    Returns (diff text, files skipped as binary or unreadable).
    """
    specs = pathspecs(root, base_dir, paths)
    listed = git(root, "ls-files", "-z", "--cached", "--others", "--exclude-standard", "--",
                 *specs, *SECRET_PATHS)
    names = list(dict.fromkeys(n for n in listed.split("\0") if n))
    parts, skipped = [], []
    for name in names:
        f = root / name
        if f.is_symlink() or not f.is_file():  # deleted in the working tree, or a submodule
            continue
        try:
            data = f.read_bytes()
        except OSError:
            skipped.append(name)
            continue
        if b"\0" in data[:8192]:
            skipped.append(name)
            continue
        lines = data.decode("utf-8", errors="replace").splitlines()
        head = f"diff --git a/{name} b/{name}\nnew file mode 100644\n--- /dev/null\n+++ b/{name}\n"
        if lines:
            head += f"@@ -0,0 +1,{len(lines)} @@\n" + "".join(f"+{x}\n" for x in lines)
        parts.append(head)
    return "".join(parts), skipped


def secret_like_lines(diff: str):
    """Every line that is sent counts: added, removed and context lines alike."""
    hits = []
    file = None
    in_hunk = False
    for line in diff.splitlines():
        if line.startswith("diff --git "):
            in_hunk = False
            file = line.split(" b/", 1)[-1]
        elif line.startswith("@@"):
            in_hunk = True
        elif not in_hunk:
            if line.startswith("+++ b/"):
                file = line[6:]
        elif line[:1] in ("+", "-", " ") and SECRET_LINE.search(line):
            hits.append({"file": file, "text": line[:120]})
    return hits


def run_model_acp(client, model: str, prompt: str, repo: Path, work: Path, timeout: int):
    """One model over ACP: plan mode, every permission request rejected (see acp.py)."""
    from .acp import Timeout
    t0 = time.time()
    try:
        res = client.prompt(prompt, model, repo, [repo, work], timeout)
    except Timeout:
        return {"model": model, "status": "TIMEOUT", "seconds": round(time.time() - t0, 1), "findings": []}
    except Fail as e:
        return {"model": model, "status": "ERROR", "error": str(e), "seconds": round(time.time() - t0, 1),
                "findings": []}
    response = res["response"]
    data = parse_answer(response)
    out = {"model": model, "seconds": res["seconds"], "status": "SUCCESS" if response else "ERROR",
           "conversation_id": res["conversation_id"], "denied_actions": res["denied_actions"],
           "findings": clean_findings((data or {}).get("findings")),
           "notes": (data or {}).get("notes") if isinstance((data or {}).get("notes"), str) else "",
           "structured": data is not None, "usage": res["usage"]}
    if data is None:
        out["response"] = response[-6000:]
    return out


def run_model(model: str, prompt: str, repo: Path, work: Path, schema_path: Path, agy: str, timeout: int,
              client=None):
    if client is not None:
        return run_model_acp(client, model, prompt, repo, work, timeout)
    cmd = [agy, "-p", prompt, "--model", model, "--mode", "plan", "--output-format", "json",
           "--json-schema", str(schema_path), "--print-timeout", f"{timeout}s",
           "--add-dir", str(repo), "--add-dir", str(work), *strict_settings_arg()]
    t0 = time.time()
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=timeout + 60, cwd=str(repo),
                           env=agy_env(), stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        return {"model": model, "status": "TIMEOUT", "seconds": round(time.time() - t0, 1), "findings": []}
    res = parse_json_tail(decode(r.stdout))
    out = {"model": model, "seconds": round(time.time() - t0, 1)}
    if res is None:
        out.update(status="ERROR", exit_code=r.returncode, stdout=decode(r.stdout)[-1500:],
                   stderr=decode(r.stderr)[-1500:], findings=[])
        return out
    response = res.get("response") if isinstance(res.get("response"), str) else ""
    data = res.get("structured_output")
    structured = isinstance(data, dict)
    if not structured:
        data = parse_answer(response) or {}
    notes = data.get("notes")
    out.update(status=res.get("status"), conversation_id=res.get("conversation_id"),
               denied_actions=res.get("denied_actions") or [],
               findings=clean_findings(data.get("findings")),
               notes=notes if isinstance(notes, str) else "",
               structured=structured, usage=res.get("usage"))
    if not structured:
        # keep the raw answer so a human can read what parsing may have missed
        out["response"] = response[-3000:]
    return out


def run_model_safe(*args):
    """One model failing must not lose the other models' results."""
    try:
        return run_model(*args)
    except Exception as e:  # noqa: BLE001 - report any failure as this model's result
        return {"model": args[0], "status": "ERROR", "error": f"{type(e).__name__}: {e}", "findings": []}


def merge(results, near: int):
    """Group findings from different models that point at the same file and nearby lines."""
    groups = []
    for r in results:
        for f in r.get("findings", []):
            f = dict(f, model=r["model"])
            for g in groups:
                if g["file"] == f.get("file") and abs(g["line"] - int(f.get("line") or 0)) <= near \
                        and r["model"] not in g["models"]:
                    g["items"].append(f)
                    g["models"].append(r["model"])
                    break
            else:
                groups.append({"file": f.get("file"), "line": int(f.get("line") or 0),
                               "models": [r["model"]], "items": [f]})
    rank = {"high": 0, "medium": 1, "low": 2}
    for g in groups:
        g["agreed"] = len(g["models"])
        g["severity"] = min((i.get("severity", "low") for i in g["items"]), key=lambda s: rank.get(s, 3))
        g["max_confidence"] = max(float(i.get("confidence") or 0) for i in g["items"])
    groups.sort(key=lambda g: (-g["agreed"], rank.get(g["severity"], 3), -g["max_confidence"]))
    for n, g in enumerate(groups, 1):
        g["id"] = n
    return groups


def review(repo: str = ".", base: str | None = None, commit: str | None = None, staged: bool = False,
           intent: str | None = None, models: list[str] | None = None, out: Path | None = None,
           near: int = 3, max_diff_kb: int = 400, allow_secret_like: bool = False,
           timeout: int = 900, client=None, paths: list[str] | None = None) -> dict:
    """Build the diff, run each model (in parallel), merge the findings, write review.json into `out`.

    paths: review these files / folders as they are (relative paths are relative to `repo`)
      instead of a change.
    client: an acp.AcpClient to ask over ACP; None runs `agy -p` with --json-schema.
    """
    if paths is not None and not isinstance(paths, list):
        raise Fail("paths: give a list of files or folders")
    if sum(bool(x) for x in (base, commit, staged, paths)) > 1:
        raise Fail("give only one of base, commit, staged, paths")
    base_dir = Path(os.path.abspath(Path(repo).expanduser()))
    root = Path(git(base_dir, "rev-parse", "--show-toplevel").strip()).resolve()
    work = Path(out).resolve()
    work.mkdir(parents=True, exist_ok=True)

    skipped: list[str] = []
    if paths:
        diff, skipped = build_files(root, base_dir, paths)
        default_intent = ""
        if not diff.strip():
            raise Fail("paths: no text files to review (missing, ignored by git, binary, "
                       "or left out as secret files)")
        too_big = f"files are larger than {max_diff_kb} KB: review fewer files or folders, or raise max_diff_kb"
    else:
        diff, default_intent = build_diff(root, SimpleNamespace(base=base, commit=commit, staged=staged))
        if not diff.strip():
            raise Fail("empty diff: nothing to review (check base / commit / staged)")
        too_big = (f"diff is larger than {max_diff_kb} KB: review it in parts (commit, or a narrower base), "
                   "or raise max_diff_kb")
    if len(diff.encode("utf-8")) > max_diff_kb * 1024:
        raise Fail(too_big)
    hits = secret_like_lines(diff)
    if hits and not allow_secret_like:
        return {"status": "STOPPED", "reason": "lines of the diff look like secrets; nothing was sent. "
                "Show them to the user; pass allow_secret_like only if they say it is fine.", "lines": hits}

    intent = (intent or default_intent or "").strip()
    diff_path = work / "diff.patch"
    diff_path.write_text(diff, encoding="utf-8")
    schema_path = work / "schema.json"
    schema_path.write_text(json.dumps(SCHEMA, ensure_ascii=False), encoding="utf-8")
    what = "the code" if paths else "the change"
    intent_text = f"Stated intent of {what} (from the author):\n{intent}\n" if intent else ""
    prompt = GUARD + (TASK_FILES if paths else TASK).format(repo=root, diff=diff_path, intent=intent_text)
    (work / "ask.txt").write_text(prompt, encoding="utf-8")

    use = list(dict.fromkeys(models or DEFAULT_MODELS))  # same model twice would overwrite its result file
    agy = agy_exe()
    with ThreadPoolExecutor(max_workers=len(use)) as ex:
        results = list(ex.map(lambda m: run_model_safe(m, prompt, root, work, schema_path, agy, timeout, client), use))
    for r in results:
        name = re.sub(r"[^A-Za-z0-9._-]", "_", r["model"])
        (work / f"result_{name}.json").write_text(json.dumps(r, ensure_ascii=False, indent=2), encoding="utf-8")

    groups = merge(results, near)
    summary = {
        "status": "SUCCESS" if any(r.get("status") == "SUCCESS" for r in results) else "ERROR",
        "repo": str(root),
        "diff": str(diff_path),
        "diff_files": len(re.findall(r"^\+\+\+ ", diff, flags=re.M)),
        **({"skipped_files": skipped} if skipped else {}),
        "models": [{k: r.get(k) for k in ("model", "status", "seconds", "denied_actions", "structured", "error")
                    if k in r} | {"findings": len(r.get("findings", []))} for r in results],
        "findings": groups,
        "agreed_by_2plus": sum(g["agreed"] >= 2 for g in groups),
        "notes": {r["model"]: r.get("notes", "") for r in results if r.get("notes")},
    }
    unparsed = {r["model"]: r["response"] for r in results if r.get("response")}
    if unparsed:  # answers with no findings JSON in them: read these yourself
        summary["unparsed"] = unparsed
    (work / "review.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    summary["review_file"] = str(work / "review.json")
    return summary
