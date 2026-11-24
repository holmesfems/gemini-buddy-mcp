"""Local media prep (ffmpeg, Pillow): cut clips, frames with a coordinate grid, audio stats, overlays.

Ported from agy-media's frames.py / audio.py / overlay.py. Every function returns a small
dict of numbers and file paths; nothing here calls a model.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

from .errors import Fail


def find_ffmpeg() -> str:
    exe = shutil.which("ffmpeg")
    if exe:
        return exe
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except ImportError:
        raise Fail("ffmpeg not found: put ffmpeg on PATH or `pip install imageio-ffmpeg`")


def need_pillow():
    try:
        from PIL import Image, ImageDraw
        return Image, ImageDraw
    except ImportError:
        raise Fail("Pillow is needed: pip install \"gemini-buddy-mcp[media]\"")


def ff_run(cmd: list[str]) -> str:
    """Run ffmpeg; return its stderr (where it prints logs) or raise with the tail of it."""
    r = subprocess.run(cmd, capture_output=True, stdin=subprocess.DEVNULL)
    err = r.stderr.decode("utf-8", "replace")
    if r.returncode:
        raise Fail(f"ffmpeg failed: {err[-1500:]}")
    return err


def duration(path: Path) -> float | None:
    """Length in seconds as ffmpeg reads it; None without ffmpeg or when it can't tell."""
    try:
        ff = find_ffmpeg()
    except Fail:
        return None
    r = subprocess.run([ff, "-hide_banner", "-i", str(path)], capture_output=True, stdin=subprocess.DEVNULL)
    m = re.search(r"Duration: (\d+):(\d+):([\d.]+)", r.stderr.decode("utf-8", "replace"))
    return round(int(m[1]) * 3600 + int(m[2]) * 60 + float(m[3]), 2) if m else None


def time_range(start: float, end: float | None) -> list[str]:
    if end is not None and end <= start:
        raise Fail(f"end ({end}) must be after start ({start})")
    return ["-ss", str(start)] + (["-to", str(end)] if end is not None else [])


def existing(path: str) -> Path:
    p = Path(path).expanduser().resolve()
    if not p.exists():
        raise Fail(f"file not found: {path}")
    return p


# ---------------------------------------------------------------- clip

def cut_clip(video: str, out: Path, start: float, end: float | None, height: int = 720,
             fps: float = 30, audio: bool = True) -> dict:
    """A small mp4 of a range (720p / 30 fps by default) that agy can watch and hear."""
    ff = find_ffmpeg()
    src = existing(video)
    out.parent.mkdir(parents=True, exist_ok=True)
    vf = f"scale=-2:{height},fps={fps}" if height else f"fps={fps}"
    cmd = [ff, "-hide_banner", "-loglevel", "error", "-y", *time_range(start, end), "-i", str(src),
           "-vf", vf, "-c:v", "libx264", "-crf", "28", "-preset", "veryfast"]
    cmd += ["-c:a", "aac", "-b:a", "160k"] if audio else ["-an"]
    ff_run(cmd + [str(out)])
    return {"path": str(out), "bytes": out.stat().st_size, "start": start, "end": end}


# ---------------------------------------------------------------- frames

def draw_grid(path: Path, step: int, ox: int, oy: int) -> None:
    """Label lines with VIDEO coordinates so the numbers can be read off directly."""
    Image, ImageDraw = need_pillow()
    im = Image.open(path).convert("RGB")
    dr = ImageDraw.Draw(im)
    w, h = im.size
    for x in range((-ox) % step, w, step):
        dr.line([(x, 0), (x, h)], fill=(255, 255, 0), width=1)
        dr.text((x + 3, 3), str(x + ox), fill=(255, 255, 0))
    for y in range((-oy) % step, h, step):
        dr.line([(0, y), (w, y)], fill=(255, 255, 0), width=1)
        dr.text((3, y + 3), str(y + oy), fill=(255, 255, 0))
    im.save(path)


def tile(images, labels, out: Path, cols: int, width: int) -> None:
    Image, ImageDraw = need_pillow()
    scale = width / images[0].width
    th = int(images[0].height * scale)
    rows = (len(images) + cols - 1) // cols
    sheet = Image.new("RGB", (cols * width, rows * th), "black")
    for i, im in enumerate(images):
        im = im.resize((width, th))
        if labels:
            ImageDraw.Draw(im).text((6, 6), labels[i], fill=(255, 0, 255))
        sheet.paste(im, ((i % cols) * width, (i // cols) * th))
    sheet.save(out)


def parse_box(text: str, n: int, sep: str, what: str) -> list[int]:
    try:
        vals = [int(float(v)) for v in str(text).split(sep)]
    except ValueError:
        vals = []
    if len(vals) != n:
        raise Fail(f"{what} must be {n} numbers separated by '{sep}', got {text!r}")
    return vals


def frame_number(path: Path) -> int:
    try:
        return int(path.stem.split("_", 1)[1])
    except (IndexError, ValueError):
        return 10 ** 9


def frames(video: str, out: Path, start: float = 0.0, end: float | None = None, fps: float = 2.0,
           crop: str | None = None, grid: int = 0, sheet: bool = False) -> dict:
    """Still frames of a range plus index.json (each frame's time in the source and the crop offset)."""
    ff = find_ffmpeg()
    src = existing(video)
    if fps <= 0:
        raise Fail("fps must be > 0")
    fdir = out / "frames"
    fdir.mkdir(parents=True, exist_ok=True)
    for old in fdir.glob("f_*.png"):
        old.unlink()
    vf = [f"fps={fps}"]
    cx = cy = 0
    if crop:
        cw, ch, cx, cy = parse_box(crop, 4, ":", "crop (W:H:X:Y)")
        vf.append(f"crop={cw}:{ch}:{cx}:{cy}")
    ff_run([ff, "-hide_banner", "-loglevel", "error", "-y", *time_range(start, end), "-i", str(src),
            "-vf", ",".join(vf), str(fdir / "f_%03d.png")])
    # by number: past 999 frames ffmpeg writes f_1000.png, which sorts before f_101.png as text
    files = sorted(fdir.glob("f_*.png"), key=frame_number)
    if not files:
        raise Fail("no frames were written; is the range inside the video?")
    if grid:
        for f in files:
            draw_grid(f, grid, cx, cy)
    # ffmpeg's fps filter puts frame n at t = n / fps from the range start
    rows = [{"file": f.name, "t_clip": round(i / fps, 3), "t_video": round(start + i / fps, 3)}
            for i, f in enumerate(files)]
    index = {"video": str(src), "start": start, "end": end, "fps": fps, "crop": {"x": cx, "y": cy},
             "grid": grid, "frames_dir": str(fdir.resolve()), "frames": rows}
    index_path = out / "index.json"
    index_path.write_text(json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8")
    sheet_path = None
    if sheet:
        Image, _ = need_pillow()
        sheet_path = out / "sheet.png"
        tile([Image.open(f).convert("RGB") for f in files], [f.name for f in files], sheet_path, 4, 480)
    return {"frames": len(files), "frames_dir": str(fdir.resolve()), "index": str(index_path.resolve()),
            "sheet": str(sheet_path.resolve()) if sheet_path else None,
            "first": str(files[0].resolve()), "last": str(files[-1].resolve()),
            "crop_offset": {"x": cx, "y": cy},
            "note": "coordinates on the grid are source-video pixels; video_x = image_x + crop_offset.x"}


# ---------------------------------------------------------------- audio

def audio(src_path: str, out: Path, start: float = 0.0, end: float | None = None,
          silence_db: float = -35.0, silence_min: float = 0.25, width: int = 1600) -> dict:
    """audio.wav (mono 16 kHz), wave.png, spectrum.png and stats.json (loudness, sound / silence spans)."""
    ff = find_ffmpeg()
    src = existing(src_path)
    out.mkdir(parents=True, exist_ok=True)
    wav = out / "audio.wav"
    ff_run([ff, "-hide_banner", "-y", *time_range(start, end), "-i", str(src), "-vn",
            "-ac", "1", "-ar", "16000", str(wav)])
    ff_run([ff, "-hide_banner", "-y", "-i", str(wav), "-filter_complex",
            f"showwavespic=s={width}x240:colors=0x3fa7ff", "-frames:v", "1", str(out / "wave.png")])
    ff_run([ff, "-hide_banner", "-y", "-i", str(wav), "-lavfi",
            f"showspectrumpic=s={width}x480:legend=1:scale=log", str(out / "spectrum.png")])
    log = ff_run([ff, "-hide_banner", "-i", str(wav), "-af",
                  f"silencedetect=noise={silence_db}dB:d={silence_min},loudnorm=print_format=json",
                  "-f", "null", "-"])
    starts = [float(v) for v in re.findall(r"silence_start: (-?[\d.]+)", log)]
    ends = [float(v) for v in re.findall(r"silence_end: ([\d.]+)", log)]
    m = re.search(r"Duration: (\d+):(\d+):([\d.]+)", log)
    dur = int(m[1]) * 3600 + int(m[2]) * 60 + float(m[3]) if m else None

    silences = [[max(0.0, s), e] for s, e in zip(starts, ends)]
    if len(starts) > len(ends) and dur:  # silent until the end
        silences.append([starts[-1], dur])
    sounds, t = [], 0.0
    for s, e in silences:
        if s - t > 0.01:
            sounds.append([t, s])
        t = e
    if dur and dur - t > 0.01:
        sounds.append([t, dur])

    loud = {}
    lm = re.search(r"\{[^{}]*\"input_i\"[^{}]*\}", log)
    if lm:
        j = json.loads(lm.group(0))
        loud = {"integrated_lufs": float(j["input_i"]), "true_peak_dbtp": float(j["input_tp"]),
                "loudness_range_lu": float(j["input_lra"])}

    def shift(spans):
        return [[round(s + start, 3), round(e + start, 3)] for s, e in spans]

    files = {k: str((out / k).resolve()) for k in ("audio.wav", "wave.png", "spectrum.png")}
    stats = {"src": str(src), "start": start, "end": end, "duration": dur,
             "note": "times are seconds in the source file", "loudness": loud,
             "sound_spans": shift(sounds), "silence_spans": shift(silences), "files": files}
    stats_path = out / "stats.json"
    stats_path.write_text(json.dumps(stats, ensure_ascii=False, indent=2), encoding="utf-8")
    return {**stats, "stats": str(stats_path.resolve())}


# ---------------------------------------------------------------- overlay

COLORS = [(255, 0, 255), (0, 255, 0), (0, 200, 255), (255, 160, 0)]


def load_track(item) -> list[dict]:
    """A track is a JSON file path ({"track": [...]} or a bare list) or the list itself."""
    if isinstance(item, (str, Path)):
        data = json.loads(existing(str(item)).read_text(encoding="utf-8-sig"))
    else:
        data = item
    if isinstance(data, dict):
        data = data.get("track", data.get("points"))
    if not isinstance(data, list):
        raise Fail("a track must be a list of points or a JSON file with a 'track' list")
    return [p for p in data if isinstance(p, dict)]


def point_for(track: list[dict], fr: dict, half: float) -> dict | None:
    for p in track:
        if p.get("frame") and Path(str(p["frame"])).stem == Path(fr["file"]).stem:
            return p
    timed = [p for p in track if "t" in p and "frame" not in p]
    if timed:
        best = min(timed, key=lambda p: abs(float(p["t"]) - fr["t_video"]))
        if abs(float(best["t"]) - fr["t_video"]) <= half:
            return best
    return None


def overlay(index_path: str, tracks: list, out: Path, space: str = "video", zoom: str | None = None,
            cols: int = 4, width: int = 600) -> dict:
    """Draw points on the frames from `frames` and tile them, to check positions by eye."""
    Image, ImageDraw = need_pillow()
    idx = json.loads(existing(index_path).read_text(encoding="utf-8-sig"))
    fdir = Path(idx["frames_dir"])
    ox, oy = idx["crop"]["x"], idx["crop"]["y"]
    half = 0.5 / idx["fps"]
    loaded = [load_track(t) for t in tracks]
    if not loaded:
        raise Fail("give at least one track")
    zx = zy = zw = zh = None
    if zoom:
        zx, zy, zw, zh = parse_box(zoom, 4, ":", "zoom (X:Y:W:H)")
    tiles, hits = [], [0] * len(loaded)
    for fr in idx["frames"]:
        im = Image.open(fdir / fr["file"]).convert("RGB")
        dr = ImageDraw.Draw(im)
        label = [fr["file"], f"t={fr['t_video']}"]
        for k, tr in enumerate(loaded):
            p = point_for(tr, fr, half)
            col = COLORS[k % len(COLORS)]
            if not p or p.get("x") is None or p.get("y") is None:
                label.append(f"#{k + 1}: -")
                continue
            hits[k] += 1
            x, y = float(p["x"]), float(p["y"])
            if space == "video":
                x, y = x - ox, y - oy
            dr.ellipse([x - 14, y - 14, x + 14, y + 14], outline=col, width=4)
            dr.line([x - 30, y, x + 30, y], fill=col, width=2)
            dr.line([x, y - 30, x, y + 30], fill=col, width=2)
            label.append(f"#{k + 1}: ({p['x']},{p['y']})")
        if zoom:
            im = im.crop((zx, zy, zx + zw, zy + zh))
        ImageDraw.Draw(im).text((8, 8), "  ".join(label), fill=(255, 255, 0))
        tiles.append(im)
    out.parent.mkdir(parents=True, exist_ok=True)
    tile(tiles, None, out, cols, width)
    return {"path": str(out.resolve()), "frames": len(tiles),
            "points_drawn": [{"track": k + 1, "color": COLORS[k % len(COLORS)], "frames_with_point": n}
                             for k, n in enumerate(hits)]}
