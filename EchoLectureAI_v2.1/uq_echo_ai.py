from __future__ import annotations

import argparse
import asyncio
import base64
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
import getpass
from copy import deepcopy
from urllib.parse import urljoin, urlparse
from datetime import datetime
from pathlib import Path
from typing import Optional, Iterable

from runtime_support import (TaskControl, TaskStopped, atomic_text, atomic_bytes,
    read_json, nonempty_text, valid_vtt, valid_mp4, fingerprint, cache_matches,
    save_cached, checked_ai_text, redact)

# Analysis, configuration and tests can run without loading the browser driver.
def async_playwright():
    from playwright.async_api import async_playwright as start
    return start()

try:
    from playwright.async_api import TimeoutError as PlaywrightTimeoutError
except ImportError:
    PlaywrightTimeoutError = TimeoutError

CONTROL = TaskControl()

# Public app paths: bundled resources are read-only; user settings/credentials live in AppData.
BUNDLE_ROOT = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
if os.getenv("ECHOLECTURE_PORTABLE", "").strip() == "1":
    _portable_base = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent
    APP_HOME = _portable_base / "data"
else:
    _local_appdata = os.getenv("LOCALAPPDATA", "").strip()
    APP_HOME = (Path(_local_appdata) if _local_appdata else Path.home() / ".local" / "share") / "EchoLectureAI"
APP_HOME.mkdir(parents=True, exist_ok=True)
ROOT = APP_HOME
DEFAULT_CONFIG_PATH = BUNDLE_ROOT / "config.default.json"
CONFIG_PATH = APP_HOME / "config.json"
PROFILE_DIR = APP_HOME / ".browser_profile"
PROMPT_PATH = BUNDLE_ROOT / "prompts" / "lecture_summary.txt"
VISION_PROMPT_PATH = BUNDLE_ROOT / "prompts" / "video_analysis.txt"

MONTH_RE = re.compile(
    r"\b(January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{1,2},\s+20\d{2}\b",
    re.I,
)


def load_config() -> dict:
    defaults = json.loads(DEFAULT_CONFIG_PATH.read_text(encoding="utf-8-sig"))
    # Keep the two-file hotfix compatible with the 2.1.0/2.1.1 defaults file.
    defaults.setdefault("only_missing", True)
    if not CONFIG_PATH.exists():
        save_config(defaults)
        return defaults
    saved = json.loads(CONFIG_PATH.read_text(encoding="utf-8-sig"))
    if not isinstance(saved, dict):
        raise ValueError(f"Settings must contain a JSON object: {CONFIG_PATH}")
    return {**defaults, **saved}


def save_config(config: dict) -> None:
    if not isinstance(config, dict):
        raise ValueError("Settings must be a JSON object")
    payload = json.dumps(config, ensure_ascii=False, indent=2) + "\n"
    if CONFIG_PATH.exists():
        atomic_bytes(CONFIG_PATH.with_suffix(".json.bak"), CONFIG_PATH.read_bytes())
    atomic_text(CONFIG_PATH, payload)


def expand_path(value: str) -> Path:
    value = os.path.expandvars(os.path.expanduser(value))
    return Path(value).resolve()


def safe_name(s: str) -> str:
    s = re.sub(r'[<>:"/\\|?*]+', "_", s)
    return re.sub(r"\s+", "_", s).strip("._ ") or "lecture"


def normalize_date(text: str) -> str:
    m = MONTH_RE.search(text)
    if not m:
        return safe_name(text)[:40]
    raw = m.group(0)
    for fmt in ("%B %d, %Y", "%b %d, %Y"):
        try:
            return datetime.strptime(raw, fmt).strftime("%Y-%m-%d")
        except ValueError:
            pass
    return safe_name(raw)


def read_transcript(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def make_ai_input(transcript_path: Path, course_code: str, lecture_date: str, visual_analysis: str = "") -> str:
    template = PROMPT_PATH.read_text(encoding="utf-8")
    return template.format(
        course_code=course_code,
        lecture_date=lecture_date,
        transcript=read_transcript(transcript_path),
        visual_analysis=visual_analysis or "[No MP4 visual analysis available]",
    )


def load_local_env(verbose=False, config=None):
    names = ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_WORKSPACE_ID")
    chosen = next((p for p in (APP_HOME / ".env", APP_HOME / ".env.txt") if p.is_file()), None)
    if chosen:
        from dotenv import dotenv_values
        values = dotenv_values(chosen, encoding="utf-8-sig", interpolate=False)
        for name in names:
            if name in values:
                os.environ[name] = (values[name] or "").strip()
    keys = {name: os.getenv(name, "").strip() for name in names}
    if verbose:
        print(f"[ENV] Settings: {APP_HOME}")
        print(f"[AI] Provider: {(config or {}).get('ai_provider', 'openai')}")
        for name in names:
            print(f"[ENV] {name}: {'configured' if keys[name] else 'empty'}")
    return chosen, keys


def selected_provider(config: dict) -> str:
    provider = str(config.get("ai_provider", "openai")).strip().lower()
    if provider not in {"openai", "anthropic"}:
        raise ValueError(f"Unsupported ai_provider: {provider}. Use 'openai' or 'anthropic'.")
    return provider


def selected_api_key(config: dict) -> str:
    _, keys = load_local_env(verbose=False, config=config)
    provider = selected_provider(config)
    if provider == "openai":
        return keys.get("OPENAI_API_KEY", "")
    return keys.get("ANTHROPIC_API_KEY", "") or keys.get("ANTHROPIC_AUTH_TOKEN", "")


def _client_from_env(config: dict, verbose: bool = False):
    _, keys = load_local_env(verbose=verbose, config=config)
    provider = selected_provider(config)
    if provider == "openai":
        api_key = keys.get("OPENAI_API_KEY", "")
        if not api_key:
            return None
        from openai import OpenAI
        return OpenAI(api_key=api_key, timeout=120.0, max_retries=2)

    api_key = keys.get("ANTHROPIC_API_KEY", "")
    auth_token = keys.get("ANTHROPIC_AUTH_TOKEN", "")
    workspace_id = keys.get("ANTHROPIC_WORKSPACE_ID", "")
    if not api_key and not auth_token:
        return None
    from anthropic import Anthropic

    # Identity-linked / multi-workspace Anthropic API keys require the
    # anthropic-workspace-id header on every request.  A workspace-scoped key does not.
    default_headers = {"anthropic-workspace-id": workspace_id} if workspace_id else None

    # Official Anthropic API: ANTHROPIC_API_KEY. ANTHROPIC_AUTH_TOKEN is mainly for
    # compatible gateways/proxies and is only used when an API key is absent.
    if api_key:
        kwargs = {"api_key": api_key, "timeout": 120.0, "max_retries": 2}
        if default_headers:
            kwargs["default_headers"] = default_headers
        return Anthropic(**kwargs)
    base_url = os.getenv("ANTHROPIC_BASE_URL", "").strip() or None
    kwargs = {"auth_token": auth_token, "timeout": 120.0, "max_retries": 2}
    if default_headers:
        kwargs["default_headers"] = default_headers
    if base_url:
        kwargs["base_url"] = base_url
    return Anthropic(**kwargs)


def _anthropic_text(response) -> str:
    parts = []
    for block in getattr(response, "content", []) or []:
        txt = getattr(block, "text", None)
        if txt:
            parts.append(txt)
    return "\n".join(parts).strip()


def test_ai_api(config: dict) -> bool:
    """Small connectivity/auth/model test for the selected AI provider."""
    client = _client_from_env(config, verbose=True)
    if client is None:
        print("[API TEST] STOP: selected provider API credential was not loaded.")
        return False
    provider = selected_provider(config)
    model = config.get("model") or ("claude-sonnet-5" if provider == "anthropic" else "gpt-5.6-sol")
    print(f"[API TEST] Calling {provider} with model: {model}")
    try:
        if provider == "openai":
            response = client.responses.create(
                model=model,
                input="Reply with exactly API_OK",
                max_output_tokens=1024,
            )
            result = checked_ai_text(response, provider)
        else:
            response = client.messages.create(
                model=model,
                max_tokens=32,
                messages=[{"role": "user", "content": "Reply with exactly API_OK"}],
            )
            result = checked_ai_text(response, provider)
        CONTROL.check()
        print(f"[API TEST] SUCCESS. Response: {result}")
        return True
    except Exception as e:
        print(f"[API TEST] FAILED: {type(e).__name__}: {redact(e)}")
        msg = str(e).lower()
        if provider == "anthropic" and "anthropic-workspace-id is required" in msg:
            print("[API TEST] This Anthropic key is identity-linked / multi-workspace.")
            print("[API TEST] Add ANTHROPIC_WORKSPACE_ID=wrkspc_... to .env, or create a workspace-scoped API key.")
        return False


def summarize_with_ai(
    transcript_path: Path, out_path: Path, config: dict, lecture_date: str,
    visual_analysis_path: Optional[Path] = None,
) -> bool:
    CONTROL.check()
    if reusable_output(out_path, config, lambda: summary_key(transcript_path, config, visual_analysis_path)):
        print(f"[KEEP] 保留已有学习笔记，不调用 API：{out_path}")
        return True
    client = _client_from_env(config)
    visual_text = ""
    if visual_analysis_path and visual_analysis_path.exists():
        visual_text = visual_analysis_path.read_text(encoding="utf-8", errors="replace")
    prompt = make_ai_input(transcript_path, config["course_code"], lecture_date, visual_text)

    if client is None or not config.get("summarize_with_api", True):
        ai_input = out_path.with_name("AI_INPUT.md")
        write_auxiliary(ai_input, prompt, config)
        print(f"[AI] No selected-provider API key / API disabled. Wrote: {ai_input}")
        return False

    try:
        provider = selected_provider(config)
        model = config.get("model") or ("claude-sonnet-5" if provider == "anthropic" else "gpt-5.6-sol")
        print(f"[AI] Final lecture summary with {provider}:{model} ...")
        if provider == "openai":
            response = client.responses.create(
                model=model,
                input=prompt,
                max_output_tokens=int(config.get("summary_max_output_tokens", 18000)),
            )
            result = checked_ai_text(response, provider)
        else:
            response = client.messages.create(
                model=model,
                max_tokens=int(config.get("summary_max_output_tokens", 18000)),
                messages=[{"role": "user", "content": prompt}],
            )
            result = checked_ai_text(response, provider)
        CONTROL.check()
        save_cached(out_path, result, summary_key(transcript_path, config, visual_analysis_path))
        print(f"[AI] Saved: {out_path}")
        return True
    except Exception as e:
        print(f"[AI] API failed: {redact(e)}")
        ai_input = out_path.with_name("AI_INPUT.md")
        write_auxiliary(ai_input, prompt, config)
        print(f"[AI] Fallback input saved: {ai_input}")
        return False

def parse_vtt_cues(path: Path) -> list[dict]:
    """Parse Echo360 VTT into timestamped cue dictionaries."""
    raw = path.read_text(encoding="utf-8-sig", errors="replace")
    raw = re.sub(r"(?<![\d:])(\d{2}:\d{2}\.\d{3})(?![\d:])", r"00:\1", raw)
    lines = raw.splitlines()
    cues: list[dict] = []
    ts_re = re.compile(r"(?P<a>\d{2}:\d{2}:\d{2}\.\d{3})\s*-->\s*(?P<b>\d{2}:\d{2}:\d{2}\.\d{3})")

    def sec(ts: str) -> float:
        h, m, rest = ts.split(":")
        return int(h) * 3600 + int(m) * 60 + float(rest)

    i = 0
    while i < len(lines):
        m = ts_re.search(lines[i])
        if not m:
            i += 1
            continue
        start_s, end_s = sec(m.group("a")), sec(m.group("b"))
        i += 1
        text_lines = []
        while i < len(lines) and lines[i].strip():
            line = re.sub(r"<[^>]+>", "", lines[i]).strip()
            if line and not line.startswith("NOTE "):
                text_lines.append(line)
            i += 1
        text = re.sub(r"\s+", " ", " ".join(text_lines)).strip()
        if text:
            cues.append({"start": start_s, "end": end_s, "text": text})
        i += 1
    return cues


def fmt_ts(seconds: float) -> str:
    seconds = max(0, int(round(seconds)))
    h = seconds // 3600
    m = (seconds % 3600) // 60
    s = seconds % 60
    return f"{h:02d}:{m:02d}:{s:02d}"


def transcript_context(cues: list[dict], t: float, window: float = 22.0) -> str:
    parts = []
    for cue in cues:
        if cue["end"] < t - window:
            continue
        if cue["start"] > t + window:
            break
        parts.append(cue["text"])
    return re.sub(r"\s+", " ", " ".join(parts)).strip()[:2400]


def ffmpeg_exe() -> str:
    configured = os.getenv("FFMPEG_PATH", "").strip()
    if configured and Path(configured).exists():
        return configured
    system_ffmpeg = shutil.which("ffmpeg")
    if system_ffmpeg:
        return system_ffmpeg
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception as e:
        raise RuntimeError(
            "ffmpeg is unavailable. Re-run setup.bat so imageio-ffmpeg is installed, "
            "or set FFMPEG_PATH in .env."
        ) from e


def video_duration_seconds(video: Path) -> float:
    exe = ffmpeg_exe()
    proc = subprocess.run(
        [exe, "-hide_banner", "-i", str(video)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace",
        timeout=45, creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    )
    m = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", proc.stderr)
    if not m:
        return 0.0
    return int(m.group(1))*3600 + int(m.group(2))*60 + float(m.group(3))


def extract_frame(video: Path, t: float, target: Path, max_width: int = 1440, quality: int = 3) -> None:
    exe = ffmpeg_exe()
    target.parent.mkdir(parents=True, exist_ok=True)
    vf = f"scale='min({max_width},iw)':-2"
    cmd = [
        exe, "-hide_banner", "-loglevel", "error", "-y",
        "-ss", f"{max(0,t):.3f}", "-i", str(video),
        "-frames:v", "1", "-vf", vf, "-q:v", str(quality), str(target),
    ]
    CONTROL.check()
    subprocess.run(cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=45,
                   creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    CONTROL.check()
    if not target.exists() or target.stat().st_size == 0:
        raise ValueError(f"No frame could be extracted at {t:.2f} seconds")


def _image_difference(a: Path, b: Path) -> float:
    try:
        from PIL import Image, ImageChops, ImageStat
        with Image.open(a) as ia, Image.open(b) as ib:
            ia = ia.convert("L").resize((320, 180))
            ib = ib.convert("L").resize((320, 180))
            diff = ImageChops.difference(ia, ib)
            return float(ImageStat.Stat(diff).mean[0])
    except Exception:
        return 999.0


def select_visual_timestamps(video: Path, vtt: Path, config: dict) -> list[float]:
    """Select key frames across the whole lecture.

    video_max_frames <= 0 means no hard total-frame cap. Frames are still filtered by
    transcript cues, scene changes, time coverage, and minimum spacing so this is not
    a literal every-video-frame upload.
    """
    cues = parse_vtt_cues(vtt)
    duration = video_duration_seconds(video)
    if duration <= 1 and cues:
        duration = cues[-1]["end"]
    if duration <= 1:
        raise RuntimeError("Could not determine video duration")

    scene_interval = max(5, int(config.get("video_scene_scan_interval_sec", 30)))
    scene_threshold = float(config.get("video_scene_change_threshold", 12.0))
    raw_max_frames = int(config.get("video_max_frames", 0))
    unlimited_frames = raw_max_frames <= 0
    max_frames = None if unlimited_frames else max(1, raw_max_frames)
    min_gap = max(5, int(config.get("video_min_frame_gap_sec", 30)))

    scene_times: list[float] = []
    with tempfile.TemporaryDirectory(prefix="echolecture_scene_") as td:
        td = Path(td)
        prev: Optional[Path] = None
        for idx, t in enumerate(range(0, int(duration)+1, scene_interval)):
            CONTROL.progress("Scanning video scenes", idx, math.ceil(duration / scene_interval))
            thumb = td / f"s_{idx:04d}.jpg"
            try:
                extract_frame(video, float(t), thumb, max_width=480, quality=7)
            except Exception:
                continue
            if prev is None or _image_difference(prev, thumb) >= scene_threshold:
                scene_times.append(float(t))
                prev = thumb

    keyword_re = re.compile(
        r"\b(class|interface|extends|implements|super|this|constructor|code|method|variable|"
        r"compile|compiler|error|exception|runtime|cast|inherit|iterator|array|list|set|map|"
        r"hash|tree|equals|compare|comparable|toString|IntelliJ|IDE|assignment|exam|diagram)\b",
        re.I,
    )
    keyword_times: list[float] = []
    last = -10_000.0
    for cue in cues:
        if keyword_re.search(cue["text"]) and cue["start"] - last >= min_gap:
            keyword_times.append(max(0.0, cue["start"] + 1.0))
            last = cue["start"]

    coverage_interval = max(30, int(config.get("video_coverage_interval_sec", 120)))
    coverage = [float(t) for t in range(0, int(duration)+1, coverage_interval)]

    def norm(t: float) -> float:
        return min(max(0.0, float(t)), max(0.0, duration - 0.5))

    if unlimited_frames:
        selected: list[float] = []
        def add_all(times: Iterable[float], gap: float):
            for t in sorted(set(round(float(x), 3) for x in times)):
                t = norm(t)
                if all(abs(t-x) >= gap for x in selected):
                    selected.append(t)
        add_all(keyword_times, min_gap)
        add_all(scene_times, max(10, min_gap * 0.65))
        add_all(coverage, max(10, min_gap * 0.65))
        for t in (5.0, max(0.0, duration - 20.0)):
            t = norm(t)
            if all(abs(t-x) >= 10 for x in selected):
                selected.append(t)
        return sorted(selected)

    # Capped mode spreads budget across the whole lecture instead of exhausting it early.
    bands = 4 if duration >= 3600 else 2
    base_quota, remainder = divmod(max_frames, bands)
    selected: list[float] = []

    def spread(times: list[float], limit: int) -> list[float]:
        vals = sorted(set(round(float(x), 3) for x in times))
        if limit <= 0 or not vals: return []
        if len(vals) <= limit: return vals
        if limit == 1: return [vals[len(vals)//2]]
        idxs = [round(i * (len(vals)-1) / (limit-1)) for i in range(limit)]
        return [vals[i] for i in sorted(set(idxs))]

    for band in range(bands):
        lo = duration * band / bands; hi = duration * (band + 1) / bands
        quota = base_quota + (1 if band < remainder else 0)
        band_selected: list[float] = []
        def add_band(times: list[float], gap: float):
            remaining = quota - len(band_selected)
            if remaining <= 0: return
            candidates = [t for t in times if lo <= t < hi or (band == bands-1 and lo <= t <= hi)]
            for t in spread(candidates, max(remaining * 3, remaining)):
                t = norm(t)
                if all(abs(t-x) >= gap for x in band_selected):
                    band_selected.append(t)
                    if len(band_selected) >= quota: return
        add_band(keyword_times, min_gap)
        add_band(scene_times, max(10, min_gap * 0.65))
        add_band(coverage, max(10, min_gap * 0.65))
        if len(band_selected) < quota:
            needed = quota - len(band_selected)
            for i in range(needed):
                t = norm(lo + (i + 0.5) * (hi - lo) / needed)
                if all(abs(t-x) >= 10 for x in band_selected): band_selected.append(t)
        selected.extend(band_selected[:quota])
    return sorted(selected[:max_frames])

def extract_visual_frames(video: Path, vtt: Path, folder: Path, config: dict) -> tuple[list[dict], Path]:
    frames_dir = folder / "video_frames" / visual_key(video, vtt, config)[:20]
    frames_dir.mkdir(parents=True, exist_ok=True)
    cues = parse_vtt_cues(vtt)
    timestamps = select_visual_timestamps(video, vtt, config)
    manifest: list[dict] = []
    max_width = int(config.get("video_frame_max_width", 1440))

    print(f"[VIDEO] Extracting {len(timestamps)} key frames from {video.name} ...")
    for idx, t in enumerate(timestamps, 1):
        name = f"{idx:02d}_{fmt_ts(t).replace(':','-')}.jpg"
        target = frames_dir / name
        CONTROL.progress("Extracting video frames", idx, len(timestamps))
        if not target.exists() or target.stat().st_size == 0:
            extract_frame(video, t, target, max_width=max_width, quality=3)
        manifest.append({
            "index": idx,
            "time": t,
            "timestamp": fmt_ts(t),
            "file": target,
            "context": transcript_context(cues, t),
        })

    manifest_path = folder / "visual_manifest.md"
    lines = [
        f"# MP4 Visual Frame Manifest — {folder.name}", "",
        "These frames were selected from the lecture MP4 using transcript cues, scene changes, and time coverage.", "",
    ]
    for item in manifest:
        lines += [
            f"## [{item['timestamp']}] {item['file'].name}",
            f"Transcript context: {item['context'] or '[none]'}", "",
        ]
    if not (config.get("only_missing", True) and nonempty_text(manifest_path)):
        save_cached(manifest_path, "\n".join(lines), visual_key(video, vtt, config))
    print(f"[VIDEO] Visual manifest: {manifest_path}")
    return manifest, manifest_path


def _image_data_url(path: Path) -> str:
    mime = "image/jpeg" if path.suffix.lower() in {".jpg", ".jpeg"} else "image/png"
    return f"data:{mime};base64," + base64.b64encode(path.read_bytes()).decode("ascii")


def analyze_video_with_ai(video: Path, vtt: Path, folder: Path, config: dict) -> Optional[Path]:
    out = folder / "visual_analysis.md"
    CONTROL.check()
    if reusable_output(out, config, lambda: visual_key(video, vtt, config)):
        print(f"[KEEP] 保留已有画面分析，不抽帧、不调用 API：{out}")
        return out
    key = visual_key(video, vtt, config)

    manifest, manifest_path = extract_visual_frames(video, vtt, folder, config)
    client = _client_from_env(config)
    if client is None or not config.get("analyze_video_with_api", True):
        print("[VIDEO] Frames extracted, but API key / video analysis is disabled.")
        print(f"[VIDEO] You can inspect/upload frames from: {folder / 'video_frames'}")
        return None

    provider = selected_provider(config)
    model = config.get("vision_model") or config.get("model") or ("claude-sonnet-5" if provider == "anthropic" else "gpt-5.6-sol")
    detail = config.get("vision_detail", "high")
    batch_size = max(1, min(10, int(config.get("vision_batch_size", 6))))
    prompt_template = VISION_PROMPT_PATH.read_text(encoding="utf-8")
    parts_dir = folder / "visual_analysis_parts" / key[:20]
    parts_dir.mkdir(parents=True, exist_ok=True)
    part_texts = []

    print(f"[VIDEO] Analyzing MP4 frames with {provider}:{model} ({len(manifest)} frames) ...")
    for batch_no, start in enumerate(range(0, len(manifest), batch_size), 1):
        batch = manifest[start:start+batch_size]
        part_path = parts_dir / f"part_{batch_no:02d}.md"
        CONTROL.progress("Analyzing video frames", batch_no, math.ceil(len(manifest)/batch_size))
        if nonempty_text(part_path):
            part_texts.append(part_path.read_text(encoding="utf-8", errors="replace"))
            continue
        content = [{
            "type": "input_text",
            "text": prompt_template.format(
                course_code=config.get("course_code", "course"),
                lecture_date=folder.name,
                frame_list="\n\n".join(
                    f"FRAME {x['index']} [{x['timestamp']}]\nTranscript context: {x['context'] or '[none]'}"
                    for x in batch
                ),
            ),
        }]
        for item in batch:
            content.append({"type": "input_text", "text": f"FRAME {item['index']} [{item['timestamp']}]"})
            content.append({
                "type": "input_image",
                "image_url": _image_data_url(item["file"]),
                "detail": detail,
            })
        try:
            if provider == "openai":
                response = client.responses.create(
                    model=model,
                    input=[{"role": "user", "content": content}],
                    max_output_tokens=int(config.get("vision_batch_max_output_tokens", 5000)),
                )
                txt = checked_ai_text(response, provider)
            else:
                anth_content = [{
                    "type": "text",
                    "text": prompt_template.format(
                        course_code=config.get("course_code", "course"),
                        lecture_date=folder.name,
                        frame_list="\n\n".join(
                            f"FRAME {x['index']} [{x['timestamp']}]\nTranscript context: {x['context'] or '[none]'}"
                            for x in batch
                        ),
                    ),
                }]
                for item in batch:
                    media_type = "image/jpeg" if item["file"].suffix.lower() in {".jpg", ".jpeg"} else "image/png"
                    anth_content.append({"type": "text", "text": f"FRAME {item['index']} [{item['timestamp']}]"})
                    anth_content.append({
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": media_type,
                            "data": base64.b64encode(item["file"].read_bytes()).decode("ascii"),
                        },
                    })
                response = client.messages.create(
                    model=model,
                    max_tokens=int(config.get("vision_batch_max_output_tokens", 5000)),
                    messages=[{"role": "user", "content": anth_content}],
                )
                txt = checked_ai_text(response, provider)
            CONTROL.check()
            atomic_text(part_path, txt)
            part_texts.append(txt)
            print(f"[VIDEO] Analyzed batch {batch_no}/{math.ceil(len(manifest)/batch_size)}")
        except Exception as e:
            print(f"[VIDEO] Vision API failed on batch {batch_no}: {redact(e)}")
            print(f"[VIDEO] Frame manifest remains available: {manifest_path}")
            return None

    header = (
        f"# MP4 Visual Analysis — {config.get('course_code','Course')} {folder.name}\n\n"
        "This file is derived from sampled lecture video frames plus nearby VTT context. "
        "It is intended to correct/augment the transcript, especially code, IDE errors, diagrams, and on-screen text.\n\n"
    )
    CONTROL.check()
    save_cached(out, header + "\n\n---\n\n".join(part_texts), key)
    print(f"[VIDEO] Saved: {out}")
    return out


def analyze_existing_lectures(config: dict, specific_folder: Optional[Path] = None):
    load_local_env(verbose=True, config=config)
    scan_root = specific_folder or expand_path(config.get("output_root", config.get("output_dir", "~/Documents/EchoLectureAI")))
    folders = [specific_folder] if specific_folder else local_lecture_folders(scan_root)
    if not folders:
        print(f"[ERROR] 未找到课程文件夹：{scan_root}")
        return False
    success = True
    for i, folder in enumerate(folders, 1):
        CONTROL.progress(f"Lecture {i}/{len(folders)}: {folder.name}", i, len(folders))
        local_cfg = deepcopy(config)
        local_cfg["course_code"] = _course_code_from_text(folder.parent.name, config.get("course_code", "COURSE"))
        if not analyze_folder(folder, local_cfg):
            success = False
    return success



async def launch_context(pw, config: dict):
    channel = config.get("browser_channel", "msedge")
    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    try:
        return await pw.chromium.launch_persistent_context(
            user_data_dir=str(PROFILE_DIR),
            channel=channel,
            headless=False,
            accept_downloads=True,
            viewport={"width": 1500, "height": 950},
        )
    except Exception as e:
        print(f"Could not launch {channel}: {redact(e)}")
        print("Falling back to Playwright Chromium. If missing, run: playwright install chromium")
        return await pw.chromium.launch_persistent_context(
            user_data_dir=str(PROFILE_DIR),
            headless=False,
            accept_downloads=True,
            viewport={"width": 1500, "height": 950},
        )


async def wait_for_login(page: Page, context, config: dict) -> Page:
    print("\n[START] Edge has opened.", flush=True)
    login_url = config.get("login_url") or config.get("courses_url") or "https://echo360.net.au/courses"
    print("[START] Opening the my.UQ Dashboard first (required for your Echo360 access).", flush=True)
    print(login_url, flush=True)
    try:
        await page.goto(login_url, wait_until="commit", timeout=30_000)
        print("[OK] my.UQ navigation started.", flush=True)
    except Exception as e:
        print(f"[WARN] Dashboard navigation did not finish quickly: {redact(e)}", flush=True)
        print("[WARN] Open https://portal.my.uq.edu.au/ manually in this Edge window.", flush=True)

    print("\nIn the Edge window:", flush=True)
    print("1) Log in to my.UQ / complete MFA.", flush=True)
    print("2) Open Learn.UQ (Blackboard).", flush=True)
    print(f"3) Open {config.get('course_code', 'your course')}.", flush=True)
    print("4) Course Resources -> Lecture Recordings.", flush=True)
    print("5) Wait until the Echo360 Class List with lecture dates is visible.", flush=True)
    print("6) Return to the app and continue in the login panel (console: press Enter).\n", flush=True)
    await asyncio.to_thread(CONTROL.request, "login", "在 Edge 完成学校登录和 MFA，进入 Lecture Recordings。看到课堂列表后，点击下方继续。")

    # Lecture Recordings can replace the current tab or open a new one.
    # Prefer the newest page whose URL is Echo360; otherwise use the newest tab.
    pages = list(context.pages)
    echo_pages = [p for p in pages if "echo360.net.au" in (p.url or "").lower()]
    selected = echo_pages[-1] if echo_pages else (pages[-1] if pages else page)
    print(f"[OK] Using browser tab: {selected.url}", flush=True)
    if "echo360.net.au" not in (selected.url or "").lower():
        print("[WARN] I cannot see an Echo360 URL yet. If the recordings are embedded inside Learn.UQ,", flush=True)
        print("       use the Lecture Recordings link to open the full Echo360 page, then rerun if scanning fails.", flush=True)
    print("[SCAN] Looking for lecture rows...", flush=True)
    await selected.wait_for_timeout(1000)
    return selected


async def probe(page: Page, output: Path):
    data = await page.evaluate(
        """() => {
          const items = [];
          const els = [...document.querySelectorAll('button,a,[role="button"]')];
          for (const el of els) {
            const r = el.getBoundingClientRect();
            if (r.width < 2 || r.height < 2) continue;
            items.push({
              tag: el.tagName,
              text: (el.innerText || '').trim().slice(0,200),
              ariaLabel: el.getAttribute('aria-label'),
              title: el.getAttribute('title'),
              role: el.getAttribute('role'),
              href: el.getAttribute('href'),
              className: typeof el.className === 'string' ? el.className.slice(0,250) : '',
              x: Math.round(r.x), y: Math.round(r.y), w: Math.round(r.width), h: Math.round(r.height)
            });
          }
          return {url: location.href, title: document.title, items};
        }"""
    )
    output.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Probe saved: {output}")


async def lecture_headers(page: Page) -> list[Locator]:
    """Return visible Echo360 class headers that contain a lecture date."""
    headers = page.locator('header.header')
    await headers.first.wait_for(state='visible', timeout=15_000)
    count = await headers.count()
    result = []
    for i in range(count):
        h = headers.nth(i)
        try:
            if not await h.is_visible():
                continue
            text = (await h.inner_text()).strip()
        except Exception:
            continue
        if MONTH_RE.search(text):
            result.append(h)
    return result


async def menu_opener_for_header(header: Locator, page: Page) -> Optional[Locator]:
    """Find the Video menu button belonging to this class header.

    Current UQ Echo360 renders the header and button as siblings.  We first
    search their shared row container; if that fails, select the visible
    button.menu-opener whose vertical centre is closest to the header.
    """
    token = 'uqai-menu-' + uuid.uuid4().hex
    try:
        selected = await header.evaluate(
            """(el, token) => {
              // Preferred: find one menu-opener inside a nearby shared ancestor.
              let p = el.parentElement;
              for (let i = 0; i < 8 && p; i++, p = p.parentElement) {
                const menus = [...p.querySelectorAll('button.menu-opener')]
                  .filter(b => {
                    const r = b.getBoundingClientRect();
                    return r.width > 2 && r.height > 2;
                  });
                if (menus.length === 1) {
                  menus[0].setAttribute('data-uqai-menu', token);
                  return true;
                }
              }

              // Fallback: Echo360 class rows align header + menu by Y position.
              const hr = el.getBoundingClientRect();
              const hc = hr.top + hr.height / 2;
              let best = null;
              let bestDist = 99999;
              for (const b of document.querySelectorAll('button.menu-opener')) {
                const r = b.getBoundingClientRect();
                if (r.width < 2 || r.height < 2) continue;
                const bc = r.top + r.height / 2;
                const d = Math.abs(bc - hc);
                if (d < bestDist) { best = b; bestDist = d; }
              }
              if (best && bestDist < 35) {
                best.setAttribute('data-uqai-menu', token);
                return true;
              }
              return false;
            }""",
            token,
        )
        if selected:
            loc = page.locator(f'[data-uqai-menu="{token}"]')
            if await loc.count() == 1:
                return loc
    except Exception as e:
        print(f'  [DEBUG] menu lookup error: {redact(e)}')
    return None


DOWNLOAD_ORIGINAL_RE = re.compile(
    r"(download\s*original|original\s*download|下载.*原始|原始.*下载|下載.*原始|原始.*下載)",
    re.I,
)


async def first_visible_matching_text(page: Page, pattern: re.Pattern) -> Optional[Locator]:
    """Return the first visible element whose rendered text matches pattern."""
    candidates = page.locator('button,a,[role="menuitem"],[role="button"],li,div,span').filter(has_text=pattern)
    n = await candidates.count()
    for i in range(min(n, 80)):
        el = candidates.nth(i)
        try:
            if not await el.is_visible():
                continue
            text = (await el.inner_text()).strip()
            if pattern.search(text):
                return el
        except Exception:
            continue
    return None




async def exact_visible_text_element(page: Page, texts: list[str], marker_attr: str) -> Optional[Locator]:
    """Find the smallest visible DOM element whose normalized text exactly matches one of texts.

    This deliberately avoids broad `has_text` ancestor matches.  Echo360's class list
    contains large containers whose descendant text includes menu labels; clicking one of
    those ancestors can navigate to the wrong scheduled class.
    """
    token = 'uqai-' + uuid.uuid4().hex
    wanted = [re.sub(r'\s+', ' ', t).strip().lower() for t in texts]
    try:
        ok = await page.evaluate(
            """({wanted, markerAttr, token}) => {
              const norm = s => (s || '').replace(/\\s+/g, ' ').trim().toLowerCase();
              const visible = el => {
                const r = el.getBoundingClientRect();
                const cs = getComputedStyle(el);
                return r.width > 2 && r.height > 2 && cs.display !== 'none' &&
                       cs.visibility !== 'hidden' && Number(cs.opacity || '1') !== 0;
              };
              const candidates = [];
              for (const el of document.querySelectorAll('button,a,li,label,span,div,[role=menuitem],[role=button],[role=link],[role=radio]')) {
                if (!visible(el)) continue;
                const txt = norm(el.innerText || el.textContent);
                if (!wanted.includes(txt)) continue;
                const r = el.getBoundingClientRect();
                candidates.push({el, area: r.width * r.height, depth: (() => {let d=0,p=el; while(p){d++;p=p.parentElement;} return d;})()});
              }
              if (!candidates.length) return false;
              candidates.sort((a,b) => a.area - b.area || b.depth - a.depth);
              candidates[0].el.setAttribute(markerAttr, token);
              return true;
            }""",
            {"wanted": wanted, "markerAttr": marker_attr, "token": token},
        )
        if ok:
            loc = page.locator(f'[{marker_attr}="{token}"]')
            if await loc.count() == 1:
                return loc
    except Exception as e:
        print(f'  [DEBUG] exact text lookup failed: {redact(e)}')
    return None


async def safe_click_text(page: Page, element: Locator, label: str) -> bool:
    """Click the exact matched element itself, never an arbitrary clickable ancestor."""
    try:
        box = await element.bounding_box()
        if not box:
            print(f'  [DEBUG] {label} has no bounding box')
            return False
        await element.scroll_into_view_if_needed(timeout=3000)
        # A real pointer click on the exact text/icon node bubbles to Echo360's handler.
        await page.mouse.click(box['x'] + box['width']/2, box['y'] + box['height']/2)
        return True
    except Exception as e:
        print(f'  [DEBUG] click {label} failed: {redact(e)}')
        return False


async def save_menu_probe(page: Page, path: Path):
    """Capture visible menu/dialog-ish DOM after a click."""
    data = await page.evaluate(
        """() => {
          const selectors = [
            'button','a','li','[role="menuitem"]','[role="menu"]','[role="button"]',
            '[role="dialog"]','[aria-haspopup]','[class*="menu" i]','[class*="popover" i]'
          ];
          const seen = new Set();
          const items = [];
          for (const sel of selectors) {
            for (const el of document.querySelectorAll(sel)) {
              if (seen.has(el)) continue; seen.add(el);
              const r = el.getBoundingClientRect();
              const cs = getComputedStyle(el);
              if (r.width < 2 || r.height < 2 || cs.visibility === 'hidden' || cs.display === 'none') continue;
              const txt = (el.innerText || el.textContent || '').trim().replace(/\\s+/g,' ').slice(0,300);
              if (!txt && !el.getAttribute('aria-label') && !el.getAttribute('title')) continue;
              items.push({
                tag: el.tagName,
                text: txt,
                ariaLabel: el.getAttribute('aria-label'),
                title: el.getAttribute('title'),
                role: el.getAttribute('role'),
                className: typeof el.className === 'string' ? el.className.slice(0,250) : '',
                x: Math.round(r.x), y: Math.round(r.y), w: Math.round(r.width), h: Math.round(r.height)
              });
            }
          }
          return {url: location.href, title: document.title, items};
        }"""
    )
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')


async def open_media_menu(header: Locator, page: Page) -> Optional[Locator]:
    """Open this lecture's Video menu and return the real Download Original menuitem.

    UQ Echo360 exposes the command as an <a role="menuitem"> element.  Using the
    accessible menuitem itself is substantially safer than matching ancestor text or
    clicking by coordinates.
    """
    button = await menu_opener_for_header(header, page)
    if button is None:
        print('  Could not locate button.menu-opener for this lecture.')
        return None

    try:
        label = await button.get_attribute('aria-label')
        print(f'  [MENU] {label or "Video menu"}')
        await button.scroll_into_view_if_needed(timeout=3000)
        await button.click(timeout=5000)
        await page.wait_for_timeout(400)
    except Exception as e:
        print(f'  [DEBUG] Video menu click failed: {redact(e)}')
        return None

    # Save the exact opened state for diagnostics.
    try:
        await save_menu_probe(page, ROOT / 'class_menu_probe.json')
    except Exception:
        pass

    # The UQ Echo360 menuitem has aria-label="".  That empty ARIA label means
    # Playwright's get_by_role(..., name=...) can see the role but may expose an
    # empty accessible name even though innerText is "下载原始".  Match the real
    # <a role="menuitem"> element by its rendered text instead.
    wanted = {
        'download original',
        '下载原始',
        '下载原始文件',
        '下載原始',
        '下載原始檔案',
    }
    try:
        items = page.locator('a[role="menuitem"], [role="menuitem"]')
        n = await items.count()
        for i in range(n):
            item = items.nth(i)
            try:
                if not await item.is_visible():
                    continue
                text = re.sub(r'\s+', ' ', (await item.inner_text()).strip()).lower()
                if text in wanted:
                    print(f'  [MENUITEM] {text}')
                    return item
            except Exception:
                continue
    except Exception as e:
        print(f'  [DEBUG] menuitem text lookup failed: {redact(e)}')

    print('  [PROBE] Video menu is open but Download Original menuitem was not found.')
    print(f'  [PROBE] Upload: {ROOT / "class_menu_probe.json"}')
    try:
        await page.keyboard.press('Escape')
    except Exception:
        pass
    return None


async def _download_lower_video_on_page(page: Page, header: Locator, folder: Path, quality: str = "Lower") -> Optional[Path]:
    """Download the authorized MP4 from Class List -> Video -> Download Original.

    v0.9 clicks the actual role=menuitem element by innerText (the UQ item has aria-label="") and refuses to continue if a click
    unexpectedly navigates away from the Class List.
    """
    prefix = "lecture_lower" if quality.lower() == "lower" else "lecture_full"
    existing = existing_video(folder, quality)
    if existing:
        print(f"  Video: {existing.name} (validated)")
        return existing

    section_url = page.url
    download_item = await open_media_menu(header, page)
    if download_item is None:
        print("  Could not find exact Download Original in this lecture's Video menu.")
        return None

    try:
        # Click the actual role=menuitem element.  Do not click parent text/coordinates.
        await download_item.click(timeout=5000)
        await page.wait_for_timeout(700)

        # Guard against the v0.6 failure mode: an ambiguous ancestor click navigated
        # into a scheduled class.  Never probe or click quality controls on the wrong page.
        if '/section/' not in page.url or '/lesson/' in page.url:
            bad = ROOT / 'unexpected_navigation_probe.json'
            try:
                await save_menu_probe(page, bad)
            except Exception:
                pass
            print(f'  [SAFE STOP] Download Original unexpectedly navigated to: {page.url}')
            print(f'  [PROBE] Upload: {bad}')
            try:
                await page.goto(section_url, wait_until='domcontentloaded', timeout=120_000)
                await page.wait_for_timeout(700)
            except Exception:
                pass
            return None

        # Full / Lower are rendered inside Download Assets.  Wait briefly for the
        # dialog/panel to appear before probing its DOM.
        try:
            await page.get_by_text(re.compile(r'^(Full|Lower)$', re.I), exact=True).first.wait_for(state='visible', timeout=7000)
        except Exception:
            pass

        q = None
        # Prefer a clickable element with accessible name Lower/Full.
        for role in ('button', 'link', 'radio'):
            try:
                cand = page.get_by_role(role, name=re.compile(rf'^{re.escape(quality)}$', re.I))
                for i in range(await cand.count()):
                    el = cand.nth(i)
                    if await el.is_visible():
                        q = el
                        break
            except Exception:
                pass
            if q is not None:
                break

        # Some Echo360 builds put the word inside a clickable card with no useful role.
        if q is None:
            q = await exact_visible_text_element(page, [quality], 'data-uqai-quality')

        if q is None:
            dp = ROOT / 'download_assets_probe.json'
            await save_menu_probe(page, dp)
            print(f'  [PROBE] Download Assets opened but exact {quality} was not found. Upload: {dp}')
            try:
                await page.keyboard.press('Escape')
            except Exception:
                pass
            return None

        async with page.expect_download(timeout=120_000) as info:
            try:
                await q.click(timeout=5000)
            except Exception:
                if not await safe_click_text(page, q, quality):
                    raise RuntimeError(f'Could not click {quality}')
        download = await info.value
        target = folder / f"{prefix}.mp4"
        await save_download(download, target, page.context, section_url, video=True)
        print(f"  Video: {target.name}")
        try:
            await page.keyboard.press("Escape")
        except Exception:
            pass
        return target
    except Exception as e:
        print(f"  Video download failed: {redact(e)}")
        try:
            await page.keyboard.press("Escape")
        except Exception:
            pass
        return None


def existing_video(folder: Path, quality: str = "Lower") -> Optional[Path]:
    prefix = "lecture_lower" if quality.lower() == "lower" else "lecture_full"
    for suffix in (".mp4", ".m4v", ".mov"):
        path = folder / (prefix + suffix)
        if valid_mp4(path):
            return path
    return None


def reusable_output(path: Path, config: dict, cache_key) -> bool:
    """Missing-only mode trusts final text, including legacy outputs without metadata.

    Evaluate a fingerprint only in strict mode; reusing a finished output must not
    require its old source files, model, timestamps or cache sidecars.
    """
    return nonempty_text(path) and (config.get("only_missing", True) or cache_matches(path, cache_key()))


def write_auxiliary(path: Path, text: str, config: dict):
    if not (config.get("only_missing", True) and nonempty_text(path)):
        atomic_text(path, text)


def lecture_work(folder: Path, config: dict) -> dict:
    """One read-only plan shared by preview, download and analysis."""
    video = existing_video(folder, config.get("video_quality", "Lower"))
    vtt = folder / "transcript.vtt"
    prefix = "lecture_lower" if config.get("video_quality", "Lower").lower() == "lower" else "lecture_full"
    video_input = video or folder / (prefix + ".mp4")
    wants_visual = config.get("analyze_video", True)
    wants_summary = config.get("summarize_with_api", True)
    visual_api = config.get("analyze_video_with_api", True)
    visual_out = folder / ("visual_analysis.md" if visual_api else "visual_manifest.md")
    has_visual = wants_visual and reusable_output(visual_out, config, lambda: visual_key(video_input, vtt, config))
    visual = visual_out if has_visual and visual_api else None
    summary_input = folder / "visual_analysis.md" if wants_visual and visual_api else None
    summary = folder / "summary.md"
    has_summary = wants_summary and reusable_output(summary, config, lambda: summary_key(vtt, config, summary_input))
    # The legacy --summarize command used this filename for transcript.vtt.
    if wants_summary and not has_summary and config.get("only_missing", True):
        alternate = folder / "transcript_summary.md"
        if nonempty_text(alternate):
            summary, has_summary = alternate, True
    needs_visual = wants_visual and not has_visual
    needs_summary = wants_summary and not has_summary
    missing, blocked = [], []
    if (config.get("download_video", True) or needs_visual) and not video:
        missing.append(prefix + ".mp4")
        if config.get("only_missing", True):
            for suffix in (".mp4", ".m4v", ".mov"):
                path = folder / (prefix + suffix)
                if path.exists() and (not path.is_file() or path.stat().st_size > 0):
                    blocked.append(path.name)
    if (config.get("download_vtt", True) or needs_visual or needs_summary) and not valid_vtt(vtt):
        missing.append(vtt.name)
        if config.get("only_missing", True) and vtt.exists() and (not vtt.is_file() or vtt.stat().st_size > 0):
            blocked.append(vtt.name)
    if needs_visual:
        missing.append(visual_out.name)
    if needs_summary:
        missing.append("summary.md")
    return {"video": video, "vtt": vtt, "visual": visual,
            "summary": summary if has_summary else None,
            "needs_visual": needs_visual, "needs_summary": needs_summary,
            "missing": missing, "blocked": blocked}


def lecture_assets_complete(folder: Path, config: dict) -> bool:
    return not lecture_work(folder, config)["missing"]


def local_lecture_folders(root: Path) -> list[Path]:
    """Also find partially downloaded lectures that do not yet have a VTT."""
    if not root.is_dir():
        return []
    markers = {"transcript.vtt", "summary.md", "transcript_summary.md", "visual_analysis.md", "done.json"}
    markers.update(prefix + suffix for prefix in ("lecture_lower", "lecture_full") for suffix in (".mp4", ".m4v", ".mov"))
    folders = []
    ignored = {"video_frames", "visual_analysis_parts", ".browser_profile", ".venv", "__pycache__"}
    for directory, dirs, files in os.walk(root, followlinks=False):
        CONTROL.check()
        path = Path(directory)
        dirs[:] = [d for d in dirs if d not in ignored and not (path / d).is_symlink()
                   and not getattr(path / d, "is_junction", lambda: False)()]
        if re.fullmatch(r"20\d{2}-\d{2}-\d{2}(?:__[0-9a-f]+)?", path.name) or markers.intersection(files):
            folders.append(path)
            dirs[:] = []
    return sorted(folders)


def inspect_missing_files(config: dict):
    """No browser, AI client, frame extraction or lecture-file writes."""
    root = expand_path(config.get("output_root", "~/Documents/EchoLectureAI"))
    print(f"[CHECK] 只检查本地文件，不调用 API、不修改课程文件：{root}")
    if not config.get("only_missing", True):
        print("[CHECK] 当前未勾选“仅补缺失文件”；缓存不匹配的已有分析也会列为待处理。")
    folders = local_lecture_folders(root)
    if not folders:
        print("[CHECK] 未找到本地课程文件夹。此检查无法发现网站上尚未下载的新课。")
        return []
    reports = []
    for folder in folders:
        cfg = {**config, "course_code": _course_code_from_text(folder.parent.name, config.get("course_code", "COURSE"))}
        work = lecture_work(folder, cfg)
        label = str(folder.relative_to(root)) if folder != root else folder.name
        kept = [p.name for p in (work["visual"], work["summary"]) if p is not None]
        if kept:
            print(f"[KEEP] {label}：保留 {', '.join(kept)}，这些输出不调用 API")
        if work["blocked"]:
            print(f"[CHECK] {label}：已有文件未通过校验，保留并暂停该课：{', '.join(work['blocked'])}")
        elif work["missing"]:
            print(f"[MISSING] {label}：{', '.join(work['missing'])}")
        else:
            print(f"[SKIP] {label}：所选内容已齐全，不调用 API")
        reports.append({"folder": folder, **work})
    complete = sum(not r["missing"] for r in reports)
    blocked = sum(bool(r["blocked"]) for r in reports)
    ai_pending = sum(not r["blocked"] and (r["needs_summary"] or
                     (r["needs_visual"] and config.get("analyze_video_with_api", True))) for r in reports)
    print(f"[CHECK] 共 {len(reports)} 节；已齐全 {complete} 节；待补齐 {len(reports)-complete} 节（含暂停 {blocked} 节）。")
    print(f"[CHECK] 其中 {ai_pending} 节缺少 AI 输出，执行分析时可能调用 API。本次检查 API 调用数：0。")
    return reports


async def click_view(page: Page, header: Locator) -> bool:
    # Clicking the class header itself is more reliable than the menu View item
    # in the current Echo360 UI. It is exposed as role=button / class=header.
    try:
        before = page.url
        await header.click(timeout=5000)
        await page.wait_for_timeout(1800)
        print(f"  [VIEW] {before} -> {page.url}")
        return True
    except Exception as e:
        print(f"  Header click failed: {redact(e)}")

    # Fallback to Video menu -> View.
    if not await open_media_menu(header, page):
        return False
    try:
        view = page.get_by_text("View", exact=True)
        await view.click(timeout=5000)
        try:
            await page.wait_for_url(re.compile(r"/lesson/"), timeout=20_000)
        except PlaywrightTimeoutError:
            await page.wait_for_timeout(2500)
        return True
    except Exception as e:
        print(f"  Menu View failed: {redact(e)}")
        return False


async def open_transcript_panel(page: Page) -> bool:
    # Prefer accessible labels/titles. Echo360 normally exposes a transcript control.
    candidates = [
        page.get_by_role("button", name=re.compile(r"transcript|转录|文字记录|逐字稿|稿本|文本", re.I)),
        page.locator('[aria-label*="transcript" i], [aria-label*="转录"], [aria-label*="文字记录"], [aria-label*="逐字稿"]'),
        page.locator('[title*="transcript" i], [title*="转录"], [title*="文字记录"], [title*="逐字稿"]'),
        page.locator('[data-testid*="transcript" i]'),
    ]
    for cand in candidates:
        n = await cand.count()
        for i in range(min(n, 10)):
            try:
                el = cand.nth(i)
                if await el.is_visible():
                    await el.click(timeout=4000)
                    await page.wait_for_timeout(700)
                    # Search box visible in the transcript pane on the current UI.
                    if await page.get_by_placeholder(re.compile("search", re.I)).count() > 0:
                        return True
            except Exception:
                continue
    return False


async def open_transcript_download_modal(page: Page) -> bool:
    # Try explicit transcript download labels first.
    candidates = [
        page.get_by_role("button", name=re.compile(r"download.*transcript|transcript.*download|下载.*转录|转录.*下载|下载.*文字记录|文字记录.*下载", re.I)),
        page.locator('[aria-label*="download" i], [aria-label*="下载"]'),
        page.locator('[title*="download" i], [title*="下载"]'),
    ]
    for cand in candidates:
        n = await cand.count()
        for i in range(min(n, 25)):
            el = cand.nth(i)
            try:
                if not await el.is_visible():
                    continue
                await el.click(timeout=3000)
                await page.wait_for_timeout(400)
                if await page.get_by_text("VTT", exact=True).is_visible():
                    return True
                # Wrong download button (e.g. video assets). Close modal/menu and continue.
                await page.keyboard.press("Escape")
            except Exception:
                try:
                    await page.keyboard.press("Escape")
                except Exception:
                    pass
    return False


async def download_vtt(page: Page, folder: Path) -> Optional[Path]:
    if not await open_transcript_panel(page):
        print("  Could not automatically open Transcript panel.")
        return None
    if not await open_transcript_download_modal(page):
        print("  Could not automatically find Transcript download button.")
        return None
    try:
        async with page.expect_download(timeout=60_000) as info:
            await page.get_by_text("VTT", exact=True).click()
        download = await info.value
        target = folder / "transcript.vtt"
        await save_download(download, target, page.context, page.url, video=False)
        print(f"  Transcript: {target.name}")
        return target
    except Exception as e:
        print(f"  VTT download failed: {redact(e)}")
        return None


async def process_one(page: Page, header: Locator, config: dict, out_root: Path) -> bool:
    CONTROL.check()
    text = " ".join((await header.inner_text()).split())
    folder = out_root / config.get("_lecture_folder", normalize_date(text))
    folder.mkdir(parents=True, exist_ok=True)
    work = lecture_work(folder, config)
    if not work["missing"]:
        print(f"[SKIP] {folder.name}：已有所选结果，跳过下载和 API")
        return True
    mark_status(folder, config, "running", "download")
    try:
        print(f"\n[LECTURE] {folder.name}")
        if work["blocked"]:
            raise ValueError("已有文件未通过校验，已保留，未下载或调用 API：" + ", ".join(work["blocked"]))
        if config.get("download_video", True) and not existing_video(folder, config.get("video_quality", "Lower")):
            await download_lower_video(page, header, folder, config.get("video_quality", "Lower"))
        CONTROL.check()
        if config.get("download_vtt", True) and not valid_vtt(folder / "transcript.vtt"):
            await download_vtt_in_worker(page, text, folder)
        if config.get("download_video", True) and not existing_video(folder, config.get("video_quality", "Lower")):
            mark_status(folder, config, "failed", "download", "MP4 download did not produce a complete container")
            return False
        if config.get("download_vtt", True) and not valid_vtt(folder / "transcript.vtt"):
            mark_status(folder, config, "failed", "download", "VTT download is missing or invalid")
            return False
        if lecture_assets_complete(folder, config):
            mark_status(folder, config, "completed", "complete")
            print(f"[KEEP] {folder.name}：下载已补齐，保留已有分析，不调用 API")
            return True
        return await asyncio.to_thread(analyze_folder, folder, config)
    except (TaskStopped, asyncio.CancelledError):
        mark_status(folder, config, "stopped", "interrupted")
        raise
    except Exception as exc:
        mark_status(folder, config, "failed", "download", redact(exc))
        print(f"[ERROR] {folder.name}: {redact(exc)}")
        return False



def _course_code_from_text(text: str, fallback: str = "COURSE") -> str:
    # UQ-style codes such as CSSE7023 / COMP7110 / INFS7900.
    m = re.search(r"(?<![A-Z0-9])([A-Z]{4}\d{4})(?!\d)", (text or "").upper())
    if m:
        return m.group(0)
    return safe_name(fallback).upper()[:40]


def _matches_term(text: str, term_filter: str) -> bool:
    wanted = re.sub(r"[^a-z0-9]", "", (term_filter or "").lower())
    if not wanted or wanted == "all":
        return True
    normal = re.sub(r"[^a-z0-9]+", " ", (text or "").lower())
    year = re.search(r"20\d{2}", wanted)
    term = re.search(r"(?:semester|term|s)([1-3])", wanted)
    if year and term:
        y, number = year.group(), term.group(1)
        return bool(re.search(rf"(?<!\d){y}\s*(?:semester|term|s)\s*{number}(?!\d)|(?:semester|term|s)\s*{number}\s*{y}(?!\d)", normal))
    return wanted in re.sub(r"[^a-z0-9]", "", normal)


async def discover_courses(page: Page, config: dict) -> list[dict]:
    """Discover course/section links from Echo360 Courses page.

    Echo360 typically renders course cards with anchors pointing to /section/<id>/home.
    We capture the nearest card text so the term (e.g. 2026 S2) can be filtered even
    when it is not part of the anchor's own text.
    """
    raw = await page.evaluate(
        """() => {
          const out = [];
          const seen = new Set();
          const links = [...document.querySelectorAll('a[href*="/section/"]')];
          const visible = el => {
            const r = el.getBoundingClientRect();
            const cs = getComputedStyle(el);
            return r.width > 2 && r.height > 2 && cs.display !== 'none' && cs.visibility !== 'hidden';
          };
          for (const a of links) {
            if (!visible(a)) continue;
            const href = new URL(a.href, location.href).href;
            if (seen.has(href)) continue;
            seen.add(href);
            let card = a;
            let bestText = (a.innerText || a.textContent || '').trim();
            for (let i=0; i<7 && card.parentElement; i++) {
              card = card.parentElement;
              const txt = (card.innerText || card.textContent || '').trim();
              if (txt.length >= bestText.length && txt.length <= 1800) bestText = txt;
              if (/semester|term|20[0-9]{2}|s[1-3]/i.test(txt) && txt.length <= 1800) {
                bestText = txt;
                break;
              }
            }
            out.push({href, anchorText:(a.innerText||a.textContent||'').trim(), cardText:bestText});
          }
          return {url: location.href, title: document.title, items: out};
        }"""
    )
    term_filter = str(config.get("term_filter", "2026S2"))
    candidates = []
    for item in raw.get("items", []):
        combined = f"{item.get('anchorText','')}\n{item.get('cardText','')}\n{item.get('href','')}"
        if not _matches_term(combined, term_filter):
            continue
        code = _course_code_from_text(combined, fallback=item.get("anchorText") or "COURSE")
        title = re.sub(r"\s+", " ", item.get("anchorText") or item.get("cardText") or code).strip()
        if len(title) > 160:
            title = title[:157] + "..."
        candidates.append({"course_code": code, "title": title or code, "url": item["href"], "raw_text": combined})

    # Deduplicate by section URL.
    dedup = []
    seen = set()
    for c in candidates:
        if c["url"] in seen:
            continue
        seen.add(c["url"])
        dedup.append(c)
    return dedup


async def wait_for_courses_login(page: Page, context, config: dict) -> Page:
    print("\n[START] Edge has opened.", flush=True)
    login_url = config.get("login_url") or config.get("courses_url") or "https://echo360.net.au/courses"
    courses_url = config.get("courses_url") or "https://echo360.net.au/courses"
    try:
        await page.goto(login_url, wait_until="commit", timeout=30_000)
    except Exception as e:
        print(f"[WARN] my.UQ navigation: {redact(e)}")
    print("1) Log in to your institution / complete MFA in this Edge window.")
    print("2) If needed, open any Lecture Recordings link once so Echo360 SSO is active.")
    print("3) Return to the app's login panel and continue (console: press Enter).")
    await asyncio.to_thread(CONTROL.request, "login", "在 Edge 完成 UQ 登录和 MFA，通过 Learn.UQ 打开一次 Lecture Recordings，然后点击下方“登录完成，继续”。")
    live_pages = [item for item in context.pages if not item.is_closed()]
    host = urlparse(courses_url).netloc
    candidates = [item for item in live_pages if urlparse(item.url).netloc == host]
    page = candidates[-1] if candidates else (live_pages[-1] if live_pages else await context.new_page())
    try:
        await page.goto(courses_url, wait_until="domcontentloaded", timeout=120_000)
        await page.wait_for_timeout(2500)
    except Exception as e:
        print(f"[WARN] Could not automatically open Courses URL: {redact(e)}")
        print("Open this URL manually in the same Edge window:")
        print(courses_url)
        await asyncio.to_thread(CONTROL.request, "login", "请在同一个 Edge 窗口打开 Echo360 Courses 页面。看到课程列表后，点击下方继续。")
    return page


async def process_section_page(page: Page, section_config: dict):
    out_root = expand_path(section_config["output_dir"])
    out_root.mkdir(parents=True, exist_ok=True)
    context = page.context
    texts = []
    for header in await lecture_headers(page):
        text = " ".join((await header.inner_text()).split())
        if text not in texts and await menu_opener_for_header(header, page) is not None:
            texts.append(text)
    if not texts:
        print("[WARN] No published lecture downloads found in this section")
        return {"completed": 0, "failed": 0, "skipped": 0}
    dates = [normalize_date(text) for text in texts]
    report = {"completed": 0, "failed": 0, "skipped": 0}
    limit = int(section_config.get("max_lectures_per_run", 0) or 0)
    attempted = 0
    for i, text in enumerate(texts):
        CONTROL.check()
        date = dates[i]
        # Separate different lecture titles on the same date without moving old data.
        folder_name = date if dates.count(date) == 1 else date + "__" + fingerprint([], {"title": text})[:8]
        folder = out_root / folder_name
        cfg = {**section_config, "_lecture_folder": folder_name}
        if (cfg.get("only_missing", True) and dates.count(date) > 1 and
                not folder.exists() and (out_root / date).is_dir()):
            print(f"[REVIEW] {date}：同日有多节课，旧目录无法唯一对应。已保留，不新建重复分析；请先确认旧结果对应哪节课。")
            report["failed"] += 1
            continue
        if lecture_assets_complete(folder, cfg):
            report["skipped"] += 1
            print(f"[SKIP] {cfg['course_code']}/{folder_name}：已有所选结果，跳过下载和 API")
            continue
        if limit and attempted >= limit:
            break
        attempted += 1
        CONTROL.progress(f"{cfg['course_code']} {folder_name}", i + 1, len(texts))
        ok = False
        for attempt in range(1, 3):
            CONTROL.check()
            try:
                if page.is_closed():
                    page = await context.new_page()
                if page.url != cfg["section_url"] or attempt > 1:
                    await page.goto(cfg["section_url"], wait_until="domcontentloaded", timeout=60_000)
                header = await find_header(page, text)
                if header is None:
                    raise RuntimeError("Lecture could not be relocated by its complete title")
                ok = await process_one(page, header, cfg, out_root)
                if ok:
                    break
                state = read_json(folder / "status.json", {})
                if state.get("stage") == "analysis":
                    break  # Avoid repeating a charged API failure in the same run.
            except Exception as exc:
                print(f"[RETRY] {folder_name} attempt {attempt}/2: {redact(exc)}")
                mark_status(folder, cfg, "failed", "download", redact(exc))
        report["completed" if ok else "failed"] += 1
        if not ok:
            print(f"[FAILED] {folder_name}. Continuing to the next lecture; run again to retry.")
    return report


async def batch_courses(config: dict):
    root = expand_path(config.get("output_root", "~/Documents/EchoLectureAI"))
    root.mkdir(parents=True, exist_ok=True)
    totals = {"completed": 0, "failed": 0, "skipped": 0}
    course_failures = []
    async with async_playwright() as pw:
        context = await launch_context(pw, config)
        try:
            page = context.pages[0] if context.pages else await context.new_page()
            page = await wait_for_courses_login(page, context, config)
            courses = await discover_courses(page, config)
            if not courses:
                print("[ERROR] No courses matched the selected term. Check the Courses page and term filter.")
                return False
            print(f"[COURSES] Found {len(courses)} section(s) for {config.get('term_filter', '')}")
            for i, course in enumerate(courses, 1):
                print(f"  {i}. {course['course_code']} | {course['title']}")
            if config.get("course_selection") == "prompt":
                options = [{"value": str(i), "label": course["course_code"], "detail": course["title"]}
                           for i, course in enumerate(courses, 1)]
                answer = (await asyncio.to_thread(CONTROL.request, "courses",
                          "在下方选择要处理的课程，再点击“下载所选课程”。（命令行：A=全部，或输入 1,3,4）", options)).strip()
                if answer.lower() not in {"a", "all", ""}:
                    choices = {int(n)-1 for n in re.split(r"[,;\s]+", answer) if n.isdigit()}
                    courses = [course for i, course in enumerate(courses) if i in choices]
                    if not courses:
                        print("[ERROR] No valid courses selected")
                        return False
            for index, course in enumerate(courses, 1):
                CONTROL.progress(f"Course {index}/{len(courses)}: {course['course_code']}", index, len(courses))
                out_dir = root / safe_name(course["course_code"])
                cfg = {**config, "course_code": course["course_code"], "section_url": course["url"], "output_dir": str(out_dir)}
                try:
                    if page.is_closed():
                        page = await context.new_page()
                    await page.goto(course["url"], wait_until="domcontentloaded", timeout=60_000)
                    atomic_text(out_dir / "course.json", json.dumps(course, ensure_ascii=False, indent=2))
                    report = await process_section_page(page, cfg)
                    for name in totals:
                        totals[name] += report[name]
                except Exception as exc:
                    course_failures.append({"course": course["course_code"], "error": redact(exc)})
                    print(f"[COURSE ERROR] {course['course_code']}: {redact(exc)}")
            atomic_text(root / "last_run.json", json.dumps({**totals, "course_failures": course_failures}, ensure_ascii=False, indent=2))
            print(f"[RESULT] Completed {totals['completed']}; reused {totals['skipped']}; failed {totals['failed']}; course errors {len(course_failures)}")
            return totals["failed"] == 0 and not course_failures
        finally:
            try:
                await context.close()
            except Exception:
                pass


def configure_ai(config: dict):
    print("\nConfigure AI provider/model")
    current_provider = str(config.get("ai_provider", "openai")).lower()
    provider = input(f"Provider [openai/anthropic] ({current_provider}): ").strip().lower() or current_provider
    if provider not in {"openai", "anthropic"}:
        raise SystemExit("Provider must be openai or anthropic")
    default_model = config.get("model") or ("claude-sonnet-5" if provider == "anthropic" else "gpt-5.6-sol")
    model = input(f"Text/summary model ID ({default_model}): ").strip() or default_model
    vision_default = config.get("vision_model") or model
    vision_model = input(f"Vision model ID ({vision_default}; Enter=same): ").strip() or model

    config["ai_provider"] = provider
    config["model"] = model
    config["vision_model"] = vision_model
    save_config(config)

    env_path = APP_HOME / ".env"
    existing = env_path.read_text(encoding="utf-8-sig", errors="replace") if env_path.exists() else ""
    if provider == "anthropic":
        cred_type = input("Anthropic credential [api_key/auth_token] (api_key): ").strip().lower() or "api_key"
        if cred_type not in {"api_key", "auth_token"}:
            raise SystemExit("Anthropic credential must be api_key or auth_token")
        key_name = "ANTHROPIC_AUTH_TOKEN" if cred_type == "auth_token" else "ANTHROPIC_API_KEY"
    else:
        key_name = "OPENAI_API_KEY"
    print(f"Enter {key_name}. Leave blank to keep the existing value.")
    secret = getpass.getpass(f"{key_name}: ").strip()
    if secret:
        pat = re.compile(rf"(?mi)^\s*{re.escape(key_name)}\s*=.*$")
        line = f"{key_name}={secret}"
        if pat.search(existing):
            existing = pat.sub(line, existing)
        else:
            existing = existing.rstrip() + ("\n" if existing.strip() else "") + line + "\n"
        env_path.write_text(existing, encoding="utf-8")
        print(f"[AI] Saved credential to {env_path}")

    if provider == "anthropic":
        existing = env_path.read_text(encoding="utf-8-sig", errors="replace") if env_path.exists() else existing
        current_ws_match = re.search(r"(?mi)^\s*ANTHROPIC_WORKSPACE_ID\s*=\s*(.*?)\s*$", existing)
        current_ws = current_ws_match.group(1).strip().strip('"').strip("'") if current_ws_match else ""
        hint = current_ws or "optional; required for identity-linked/multi-workspace keys"
        workspace_id = input(f"Anthropic Workspace ID ({hint}; Enter=keep/skip): ").strip()
        if workspace_id:
            if not workspace_id.startswith("wrkspc_"):
                print("[WARN] Anthropic workspace IDs normally start with wrkspc_. Saving anyway.")
            pat = re.compile(r"(?mi)^\s*ANTHROPIC_WORKSPACE_ID\s*=.*$")
            line = f"ANTHROPIC_WORKSPACE_ID={workspace_id}"
            if pat.search(existing):
                existing = pat.sub(line, existing)
            else:
                existing = existing.rstrip() + ("\n" if existing.strip() else "") + line + "\n"
            env_path.write_text(existing, encoding="utf-8")
            print(f"[AI] Saved workspace ID to {env_path}")
    print(f"[AI] Selected: {provider}:{model}; vision={vision_model}")
    print("Run test_api.bat next.")

async def batch(config: dict):
    if not config.get("section_url"):
        raise ValueError("Single-section mode requires section_url in settings")
    async with async_playwright() as pw:
        context = await launch_context(pw, config)
        try:
            page = context.pages[0] if context.pages else await context.new_page()
            page = await wait_for_login(page, context, config)
            report = await process_section_page(page, config)
            print(f"[RESULT] {report}")
            return report["failed"] == 0
        finally:
            await context.close()


async def run_probe(config: dict):
    async with async_playwright() as pw:
        context = await launch_context(pw, config)
        page = context.pages[0] if context.pages else await context.new_page()
        page = await wait_for_login(page, context, config)
        await probe(page, APP_HOME / "echo_probe.json")
        await context.close()


def summarize_local(path_str: str, config: dict):
    path = Path(path_str).resolve()
    if not path.exists():
        raise SystemExit(f"File not found: {path}")
    lecture_date = path.parent.name if re.fullmatch(r"20\d{2}-\d{2}-\d{2}", path.parent.name) else "unknown"
    visual = path.parent / "visual_analysis.md"
    out = path.with_name(path.stem + "_summary.md")
    if path.name == "transcript.vtt" and config.get("only_missing", True) and nonempty_text(path.parent / "summary.md"):
        print(f"[KEEP] 保留已有学习笔记，不调用 API：{path.parent / 'summary.md'}")
        return True
    return summarize_with_ai(path, out, config, lecture_date, visual if visual.exists() else None)


def visual_key(video, vtt, config):
    fields = ("ai_provider", "vision_model", "model", "video_max_frames", "video_min_frame_gap_sec",
              "video_scene_scan_interval_sec", "video_scene_change_threshold", "video_coverage_interval_sec",
              "video_frame_max_width", "vision_detail", "vision_batch_size", "vision_batch_max_output_tokens", "course_code")
    return fingerprint([video, vtt, VISION_PROMPT_PATH], {k: config.get(k) for k in fields})


def summary_key(vtt, config, visual=None):
    files = [vtt, PROMPT_PATH]
    if visual is not None:
        files.append(visual)
    fields = ("ai_provider", "model", "summary_max_output_tokens", "course_code", "analyze_video")
    return fingerprint(files, {k: config.get(k) for k in fields})


def mark_status(folder, config, status, stage, error=""):
    record = {"schema": 1, "status": status, "stage": stage, "course_code": config.get("course_code"),
              "lecture": folder.name, "error": redact(error), "updated_at": datetime.now().isoformat(timespec="seconds")}
    atomic_text(folder / "status.json", json.dumps(record, ensure_ascii=False, indent=2))
    if status == "completed":
        atomic_text(folder / "done.json", json.dumps({**record, "complete": True}, ensure_ascii=False, indent=2))
    elif (folder / "done.json").exists():
        atomic_text(folder / "done.json", json.dumps({**record, "complete": False}, ensure_ascii=False, indent=2))


def analyze_folder(folder, config):
    CONTROL.check()
    work = lecture_work(folder, config)
    if not work["missing"]:
        print(f"[SKIP] {folder.name}：已有所选结果，不调用 API、不改写文件")
        return True
    mark_status(folder, config, "running", "analysis")
    try:
        if work["blocked"]:
            raise ValueError("已有文件未通过校验，已保留，未调用 API：" + ", ".join(work["blocked"]))
        video, vtt = work["video"], work["vtt"]
        wants_visual = config.get("analyze_video", True)
        needs_summary = work["needs_summary"]
        for name in work["missing"]:
            if name.endswith((".mp4", ".vtt")):
                raise ValueError(f"缺少 {name}；请先使用“下载并分析课程”补齐。已有分析保持不变。")
        if work["visual"] is not None:
            print(f"[KEEP] 保留已有结果，不调用 API：{work['visual']}")
        visual = work["visual"]
        if work["needs_visual"]:
            if config.get("analyze_video_with_api", True):
                visual = analyze_video_with_ai(video, vtt, folder, config)
                if visual is None:
                    raise ValueError("Video AI analysis did not finish. Check the API message above and rerun to resume.")
            else:
                extract_visual_frames(video, vtt, folder, config)
        # A newly regenerated visual result can invalidate the summary in strict
        # mode. Missing-only mode continues to retain any existing summary.
        needs_summary = lecture_work(folder, config)["needs_summary"]
        if not needs_summary and work["summary"] is not None:
            print(f"[KEEP] 保留已有结果，不调用 API：{work['summary']}")
        if needs_summary and wants_visual and visual is None:
            raise ValueError("Video analysis is selected: summary is waiting for its visual analysis")
        CONTROL.check()
        if valid_vtt(vtt):
            visual_text = visual.read_text(encoding="utf-8-sig") if visual else ""
            write_auxiliary(folder / "AI_INPUT.md", make_ai_input(vtt, config.get("course_code", "COURSE"), folder.name, visual_text), config)
        if needs_summary:
            CONTROL.progress(f"Generating missing summary: {folder.name}")
            if not summarize_with_ai(vtt, folder / "summary.md", config, folder.name, visual):
                raise ValueError("Summary generation did not finish. Check the API message above.")
        ok = lecture_assets_complete(folder, config)
        mark_status(folder, config, "completed" if ok else "failed", "complete" if ok else "analysis",
                    "" if ok else "Some selected assets are still missing")
        return ok
    except TaskStopped:
        mark_status(folder, config, "stopped", "analysis")
        raise
    except Exception as exc:
        mark_status(folder, config, "failed", "analysis", redact(exc))
        print(f"[ANALYSIS ERROR] {folder.name}: {redact(exc)}")
        return False


async def save_download(download, target, context, referer, video=True):
    """Save only complete assets; use the exact authorized URL if save_as fails."""
    target.parent.mkdir(parents=True, exist_ok=True)
    partial = target.with_name(target.name + "." + uuid.uuid4().hex[:10] + ".part")
    url = download.url
    try:
        CONTROL.check()
        try:
            await download.save_as(str(partial))
        except Exception as original:
            if urlparse(url).scheme not in {"https", "http"}:
                raise RuntimeError("Browser download failed and its URL cannot be retried") from original
            print("[DOWNLOAD] Browser save failed; retrying its authorized URL in the same login session.")
            await stream_authorized_asset(url, context, referer, partial)
        CONTROL.check()
        valid = valid_mp4(partial) if video else valid_vtt(partial)
        if not valid:
            raise ValueError("Downloaded file is incomplete or not the expected MP4/VTT (possibly a login page)")
        os.replace(partial, target)
        return target
    except (TaskStopped, asyncio.CancelledError):
        try:
            await download.cancel()
        except Exception:
            pass
        raise
    finally:
        partial.unlink(missing_ok=True)


async def stream_authorized_asset(url, context, referer, partial):
    import httpx
    from http.cookiejar import Cookie, CookieJar
    jar = CookieJar()
    # BrowserContext.cookies(url) returns only cookies applicable to this URL.
    # CookieJar preserves their domain, path and secure restrictions on redirects.
    for item in await context.cookies([url]):
        domain = item["domain"]
        expires = item.get("expires", -1)
        jar.set_cookie(Cookie(version=0, name=item["name"], value=item["value"], port=None, port_specified=False,
            domain=domain, domain_specified=domain.startswith("."), domain_initial_dot=domain.startswith("."),
            path=item.get("path", "/"), path_specified=True, secure=item.get("secure", False),
            expires=int(expires) if expires > 0 else None, discard=expires <= 0,
            comment=None, comment_url=None, rest={}, rfc2109=False))
    clean_referer = referer.split("?", 1)[0].split("#", 1)[0]
    async with httpx.AsyncClient(cookies=jar, follow_redirects=True,
            timeout=httpx.Timeout(180, connect=30, read=60),
            headers={"Referer": clean_referer, "Accept-Encoding": "identity"}) as client:
        async with client.stream("GET", url) as response:
            response.raise_for_status()
            if response.status_code != 200:
                raise ValueError(f"Expected full download, received HTTP {response.status_code}")
            kind = response.headers.get("content-type", "").lower()
            if "html" in kind or "json" in kind:
                raise ValueError("Download endpoint returned a login/error document")
            expected = response.headers.get("content-length")
            written = 0
            with partial.open("wb") as handle:
                async for chunk in response.aiter_bytes(1024 * 1024):
                    CONTROL.check()
                    handle.write(chunk)
                    written += len(chunk)
                    CONTROL.progress("Saving download", written, int(expected) if expected and expected.isdigit() else 0)
                handle.flush()
                os.fsync(handle.fileno())
            if expected and expected.isdigit() and not response.headers.get("content-encoding") and written != int(expected):
                raise ValueError("Download ended before Content-Length was received")


async def find_header(page, text):
    # Match the entire title, not just the date; multiple classes can share a date.
    for _ in range(3):
        CONTROL.check()
        matches = []
        for header in await lecture_headers(page):
            if " ".join((await header.inner_text()).split()) == text:
                matches.append(header)
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            raise ValueError("More than one lecture has the same complete title; selection is ambiguous")
        await page.wait_for_timeout(700)
    return None


async def download_lower_video(page, header, folder, quality="Lower"):
    text = " ".join((await header.inner_text()).split())
    worker = None
    try:
        CONTROL.check()
        worker = await page.context.new_page()
        await worker.goto(page.url, wait_until="domcontentloaded", timeout=60_000)
        worker_header = await find_header(worker, text)
        if worker_header is None:
            print("[DOWNLOAD] Lecture not found in download tab; will retry without touching the class list")
            return None
        return await _download_lower_video_on_page(worker, worker_header, folder, quality)
    except Exception as exc:
        print(f"[DOWNLOAD] Worker failed: {redact(exc)}")
        return None
    finally:
        if worker is not None:
            try:
                if not worker.is_closed():
                    await worker.close()
            except Exception:
                pass


async def download_vtt_in_worker(page, text, folder):
    worker = None
    try:
        CONTROL.check()
        worker = await page.context.new_page()
        await worker.goto(page.url, wait_until="domcontentloaded", timeout=60_000)
        header = await find_header(worker, text)
        if header is None or not await click_view(worker, header):
            return None
        return await download_vtt(worker, folder)
    finally:
        if worker is not None:
            try:
                if not worker.is_closed():
                    await worker.close()
            except Exception:
                pass


def save_credentials(provider, key, workspace=""):
    env = APP_HOME / ".env"
    content = env.read_text(encoding="utf-8-sig") if env.exists() else ""
    values = {"OPENAI_API_KEY" if provider == "openai" else "ANTHROPIC_API_KEY": key.strip()}
    if provider == "anthropic":
        values["ANTHROPIC_WORKSPACE_ID"] = workspace.strip()
        values["ANTHROPIC_AUTH_TOKEN"] = ""  # Clearing the API key must not revive an old token.
    for name, value in values.items():
        if "\n" in value or "\r" in value:
            raise ValueError("API credentials must be on one line")
        line = name + "=" + json.dumps(value)
        pattern = re.compile(rf"(?m)^[ \t]*{re.escape(name)}[ \t]*=.*$")
        content = pattern.sub(lambda m: line, content) if pattern.search(content) else content.rstrip() + "\n" + line + "\n"
        os.environ[name] = value
    atomic_text(env, content)


class LegacyConfigSelectionNeeded(ValueError):
    """Ask the user to select a file when a folder cannot identify one config."""


def resolve_legacy_settings_source(selected_path):
    """Resolve a file or an extraction folder without guessing between versions.

    Prefer a config in the selected directory. Otherwise inspect at most three
    child-directory levels, skipping environments, browser data and symlinks.
    """
    selected = Path(selected_path).expanduser()
    if selected.is_file():
        if selected.suffix.lower() != ".json":
            raise LegacyConfigSelectionNeeded("请选择旧版 config.json 文件。")
        return selected
    if not selected.is_dir():
        raise LegacyConfigSelectionNeeded("所选路径不存在，请重新选择旧版 config.json。")
    direct = selected / "config.json"
    if direct.is_file():
        return direct

    ignored = {".venv", "venv", ".buildvenv", "__pycache__", ".git", "node_modules",
               "site-packages", ".browser_profile", "video_frames", "visual_analysis_parts"}
    pending = [(selected, 0)]
    candidates = []
    inspected = 0
    while pending:
        directory, depth = pending.pop(0)
        inspected += 1
        if inspected > 200:
            raise LegacyConfigSelectionNeeded("所选目录过大，请直接选择旧版 config.json 文件。")
        config = directory / "config.json"
        if depth and config.is_file() and not config.is_symlink():
            candidates.append(config)
        if depth >= 3:
            continue
        try:
            children = sorted(directory.iterdir(), key=lambda path: path.name.casefold())
            for child in children:
                if (child.name.casefold() in ignored or child.is_symlink()
                        or getattr(child, "is_junction", lambda: False)()):
                    continue
                if child.is_dir():
                    pending.append((child, depth + 1))
        except PermissionError as exc:
            raise LegacyConfigSelectionNeeded("无法读取部分子目录，请直接选择旧版 config.json 文件。") from exc

    if not candidates:
        raise LegacyConfigSelectionNeeded("没有找到 config.json，请进入旧版解压目录直接选择该文件。")
    if len(candidates) > 1:
        raise LegacyConfigSelectionNeeded("发现多个 config.json，请选择要导入的旧版配置文件。")
    return candidates[0]


def import_legacy_settings(directory):
    source = resolve_legacy_settings_source(directory)
    directory = source.parent
    old = json.loads(source.read_text(encoding="utf-8-sig"))
    if not isinstance(old, dict):
        raise ValueError("Old config.json is not a JSON object")
    current = load_config()
    merged = {**current, **{key: value for key, value in old.items() if key in current}}
    if not old.get("output_root") and old.get("output_dir"):
        merged["output_root"] = str(expand_path(old["output_dir"]).parent)
    credentials = directory / ".env"
    if not credentials.exists():
        credentials = directory / ".env.txt"
    if credentials.exists():
        from dotenv import dotenv_values
        values = dotenv_values(credentials, encoding="utf-8-sig", interpolate=False)
        if values.get("OPENAI_API_KEY"):
            save_credentials("openai", values["OPENAI_API_KEY"])
        if values.get("ANTHROPIC_API_KEY"):
            save_credentials("anthropic", values["ANTHROPIC_API_KEY"], values.get("ANTHROPIC_WORKSPACE_ID") or "")
    save_config(merged)
    return merged


def main():
    # Load .env before completeness checks so adding an API key later causes
    # already-downloaded lectures with no visual analysis / summary to be processed.
    config_pre = load_config()
    load_local_env(verbose=False, config=config_pre)
    parser = argparse.ArgumentParser(description="EchoLecture AI: Echo360 downloader + MP4/VTT AI lecture analyzer")
    parser.add_argument("--batch", action="store_true", help="Legacy single-section download using section_url")
    parser.add_argument("--courses", action="store_true", help="Scan Echo360 Courses and download all matching term courses")
    parser.add_argument("--configure-ai", action="store_true", help="Interactively choose OpenAI/Anthropic model and save API key")
    parser.add_argument("--probe", action="store_true", help="Dump visible Echo360 buttons/links for selector tuning")
    parser.add_argument("--summarize", metavar="FILE", help="Summarize a local VTT/TXT file")
    parser.add_argument("--analyze-existing", action="store_true", help="Analyze all downloaded MP4+VTT lecture folders without opening Echo360")
    parser.add_argument("--check-missing", action="store_true", help="List missing local lecture files without API calls or lecture-file changes")
    parser.add_argument("--analyze-folder", metavar="FOLDER", help="Analyze one downloaded lecture folder without opening Echo360")
    parser.add_argument("--test-api", action="store_true", help="Test selected AI provider/model and .env credential")
    args = parser.parse_args()
    config = load_config()

    if args.check_missing:
        inspect_missing_files(config)
        return 0
    if args.configure_ai:
        configure_ai(config)
        return
    if args.test_api:
        return 0 if test_ai_api(config) else 1
    if args.summarize:
        return 0 if summarize_local(args.summarize, config) else 1
    if args.analyze_folder:
        return 0 if analyze_existing_lectures(config, Path(args.analyze_folder).resolve()) else 1
    if args.analyze_existing:
        return 0 if analyze_existing_lectures(config) else 1
    if args.probe:
        asyncio.run(run_probe(config))
        return
    if args.courses or (not args.batch):
        return 0 if asyncio.run(CONTROL.run(batch_courses(config))) else 1
    return 0 if asyncio.run(CONTROL.run(batch(config))) else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (KeyboardInterrupt, TaskStopped):
        CONTROL.stop()
        print("[STOPPED] Completed results have been preserved")
        raise SystemExit(130)
