"""Local measurements: voice vs BGM in a finished mix (numpy), object tracking (OpenCV).

Ported from agy-media's mix_balance.py / track.py.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

from .errors import Fail
from .media import existing, find_ffmpeg, parse_box

SR = 16000


def need_numpy():
    try:
        import numpy as np
        return np
    except ImportError:
        raise Fail("numpy is needed: pip install \"gemini-buddy-mcp[media]\"")


# ---------------------------------------------------------------- mix balance

def load_pcm(ff: str, src: Path, start: float | None = None, end: float | None = None):
    np = need_numpy()
    rng = (["-ss", str(start)] if start is not None else []) + (["-to", str(end)] if end is not None else [])
    r = subprocess.run([ff, "-hide_banner", "-loglevel", "error", *rng, "-i", str(src), "-vn",
                        "-ac", "1", "-ar", str(SR), "-f", "s16le", "-"],
                       capture_output=True, stdin=subprocess.DEVNULL)
    if r.returncode:
        raise Fail(f"ffmpeg could not read {src}: {r.stderr.decode('utf-8', 'replace')[-800:]}")
    return np.frombuffer(r.stdout, dtype=np.int16).astype(np.float64) / 32768


def best_lag(long, short) -> int:
    """Offset k maximising |sum(long[k:k+len(short)] * short)| (FFT cross-correlation)."""
    np = need_numpy()
    if len(short) == 0 or len(long) < len(short):
        return 0
    n = len(long) + len(short)
    size = 1 << (n - 1).bit_length()
    c = np.fft.irfft(np.fft.rfft(long, size) * np.conj(np.fft.rfft(short, size)), size)
    return int(np.argmax(np.abs(c[: len(long) - len(short) + 1])))


def level_db(x) -> float:
    np = need_numpy()
    return float(20 * np.log10(np.sqrt(np.mean(x ** 2)) + 1e-12))


def mix_balance(mix_path: str, voice_path: str, start: float = 0.0, end: float | None = None,
                voice_start: float | None = None, win: float = 0.5, min_db: float = 6.0,
                out: Path | None = None) -> dict:
    """Align the voice-only track to the mix, subtract it, and compare voice with what is left (BGM + SE)."""
    np = need_numpy()
    ff = find_ffmpeg()
    mix_src, voice_src = existing(mix_path), existing(voice_path)
    mix = load_pcm(ff, mix_src, start, end)
    if len(mix) < SR:
        raise Fail("the mix range is shorter than 1 s")
    vs = voice_start if voice_start is not None else max(0.0, start - 10)
    voice = load_pcm(ff, voice_src, vs, vs + len(mix) / SR + 20)
    if not len(voice):
        raise Fail("the voice track is empty in that range")

    v = np.zeros_like(mix)
    if voice_start is not None:
        lag = best_lag(mix[: SR * 20], voice[: SR * 10])
        n = min(len(mix) - lag, len(voice))
        v[lag:lag + n] = voice[:n]
        offset = vs - (start + lag / SR)
    else:
        k = best_lag(voice, mix[: SR * 10])
        n = min(len(mix), len(voice) - k)
        v[:n] = voice[k:k + n]
        offset = vs + k / SR - start
    vv = float(np.dot(v, v))
    if vv == 0:
        raise Fail("could not place the voice track in the mix (silent voice?)")
    g = float(np.dot(mix, v) / vv)
    bgm = mix - g * v

    step = int(SR * win)
    rows = []
    for i in range(0, len(mix) - step + 1, step):
        lv, lb = level_db(g * v[i:i + step]), level_db(bgm[i:i + step])
        if lv > -45:
            rows.append({"t": round(start + i / SR, 2), "voice_db": round(lv, 1), "bgm_db": round(lb, 1),
                         "diff_db": round(lv - lb, 1)})
    buried: list[list[float]] = []
    for r in rows:
        if r["diff_db"] < min_db:
            if buried and abs(buried[-1][1] - r["t"]) < 1e-6:
                buried[-1][1] = round(r["t"] + win, 2)
            else:
                buried.append([r["t"], round(r["t"] + win, 2)])
    diffs = [r["diff_db"] for r in rows]
    result = {
        "mix": str(mix_src), "voice": str(voice_src), "range": [start, end],
        "voice_track_offset_s": round(offset, 3),
        "note": ("mix time t = voice-track time t + offset. Talk videos usually keep BGM 12-20 dB "
                 f"under the voice; buried_spans are windows where voice - BGM < {min_db} dB."),
        "voice_gain": round(g, 3),
        "mean_voice_minus_bgm_db": round(float(np.mean(diffs)), 1) if diffs else None,
        "min_voice_minus_bgm_db": min(diffs) if diffs else None,
        "buried_spans": buried,
        "windows": len(rows),
    }
    if out:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps({**result, "rows": rows}, ensure_ascii=False, indent=1), encoding="utf-8")
        result["rows_file"] = str(out.resolve())
    return result


# ---------------------------------------------------------------- tracking

def track(video: str, start: float, end: float, box: str, out: Path, search: int = 10, every: int = 1,
          update: float = 0.7, hold: float = 0.45) -> dict:
    """Follow one object by template matching; points below `hold` keep the last position ("held")."""
    try:
        import cv2
    except ImportError:
        raise Fail("OpenCV is needed: pip install \"gemini-buddy-mcp[media]\"")
    src = existing(video)
    if end <= start:
        raise Fail("end must be after start")
    every = max(1, int(every))
    cap = cv2.VideoCapture(str(src))
    if not cap.isOpened():
        raise Fail(f"OpenCV cannot open {src}")
    try:
        fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
        first, last = int(round(start * fps)), int(round(end * fps))
        cap.set(cv2.CAP_PROP_POS_FRAMES, first)
        x, y, w, h = parse_box(box, 4, ",", "box (x,y,w,h)")
        ok, frame = cap.read()
        if not ok:
            raise Fail("cannot read the start frame")
        H, W = frame.shape[:2]
        if w <= 0 or h <= 0 or x < 0 or y < 0 or x + w > W or y + h > H:
            raise Fail(f"box {box} is outside the {W}x{H} frame")
        original = frame[y:y + h, x:x + w].copy()
        if float(original.std()) < 2.0:
            raise Fail("the box is a flat colour, so matching cannot tell places apart; "
                       "widen it to take in some edges or background")
        latest = original
        points = [{"frame_index": first, "t": round(first / fps, 3), "x": x + w // 2, "y": y + h // 2,
                   "score": 1.0}]
        n = first
        while n < last:
            ok, frame = cap.read()
            n += 1
            if not ok:
                break
            if (n - first) % every:
                continue
            r = search * every
            sx0, sy0 = max(0, x - r), max(0, y - r)
            sx1, sy1 = min(W, x + w + r), min(H, y + h + r)
            area = frame[sy0:sy1, sx0:sx1]
            best = (-1.0, (x - sx0, y - sy0))
            for tpl in (original, latest):
                res = cv2.matchTemplate(area, tpl, cv2.TM_CCOEFF_NORMED)
                _, score, _, loc = cv2.minMaxLoc(res)
                if score > best[0]:
                    best = (score, loc)
            score, (lx, ly) = best
            held = score < hold
            if not held:
                x, y = sx0 + lx, sy0 + ly
            if score >= update:
                latest = frame[y:y + h, x:x + w].copy()
            p = {"frame_index": n, "t": round(n / fps, 3), "x": x + w // 2, "y": y + h // 2,
                 "score": round(float(score), 3)}
            if held:
                p["held"] = True
            points.append(p)
    finally:
        cap.release()
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"video": str(src), "fps": fps, "box_size": [w, h], "track": points},
                              ensure_ascii=False, indent=1), encoding="utf-8")
    held_n = sum(1 for p in points if p.get("held"))
    return {"path": str(out.resolve()), "points": len(points), "held_points": held_n, "fps": fps,
            "first": points[0], "last": points[-1],
            "note": "x, y are the box centre in source-video pixels; check with media_overlay"}
