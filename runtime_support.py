"""Small, dependency-free helpers shared by the GUI and download engine."""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
from pathlib import Path
import re
import struct
import tempfile
import threading
import time


class TaskStopped(BaseException):
    """A user stop must not be swallowed by a normal download retry."""


class TaskPaused(BaseException):
    """An uncertain API failure ends the batch without an automatic resubmit."""


class TaskControl:
    def __init__(self, input_handler=None, progress_handler=None, request_handler=None):
        self.stopped = threading.Event()
        self.input_handler = input_handler
        self.progress_handler = progress_handler
        self.request_handler = request_handler
        self._state_lock = threading.Lock()
        self._api_request = None
        self._sync_work = 0

    def begin_api_request(self, label, timeout):
        self.check()
        with self._state_lock:
            self._api_request = {"label": label, "timeout": timeout, "started": time.monotonic()}

    def finish_api_request(self):
        with self._state_lock:
            self._api_request = None

    def api_request_state(self):
        with self._state_lock:
            return dict(self._api_request) if self._api_request else None

    async def run_sync(self, operation, *args):
        """Keep the async owner alive until an analysis thread has saved its result."""
        self.check()
        with self._state_lock:
            self._sync_work += 1
        try:
            return await asyncio.to_thread(operation, *args)
        finally:
            with self._state_lock:
                self._sync_work -= 1

    def check(self):
        if self.stopped.is_set():
            raise TaskStopped("Task stopped by user")

    def stop(self):
        self.stopped.set()

    def ask(self, prompt=""):
        self.check()
        result = self.input_handler(prompt) if self.input_handler else input(prompt)
        self.check()
        return result

    def request(self, kind, prompt, options=None):
        """Structured GUI requests, with the original console-input fallback."""
        self.check()
        if self.request_handler is None:
            return self.ask(prompt)
        result = self.request_handler({"kind": kind, "prompt": prompt, "options": list(options or [])})
        self.check()
        return result

    def progress(self, label, current=0, total=0):
        self.check()
        if self.progress_handler:
            self.progress_handler(label, current, total)

    async def run(self, coroutine):
        task = asyncio.create_task(coroutine)
        try:
            while not task.done():
                with self._state_lock:
                    saving_in_thread = self._sync_work > 0
                if self.stopped.is_set() and not saving_in_thread:
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
                    raise TaskStopped("Task stopped by user")
                await asyncio.wait({task}, timeout=0.1)
            return await task
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)


def atomic_bytes(path: Path, data: bytes):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def atomic_text(path: Path, text: str):
    atomic_bytes(path, text.encode("utf-8"))


def read_json(path: Path, default=None):
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return default


def nonempty_text(path: Path):
    try:
        return bool(path.read_text(encoding="utf-8-sig").strip())
    except (OSError, UnicodeError):
        return False


def valid_vtt(path: Path):
    try:
        text = path.read_text(encoding="utf-8-sig")
        return text.lstrip().startswith("WEBVTT") and bool(re.search(
            r"(?:\d{2,}:)?\d{2}:\d{2}\.\d{3}\s+-->\s+(?:\d{2,}:)?\d{2}:\d{2}\.\d{3}", text))
    except (OSError, UnicodeError):
        return False


def valid_mp4(path: Path):
    """Check the ISO BMFF container boundary, including truncated downloads.

    This checks container completeness, not decoding of every video frame.
    """
    try:
        size = path.stat().st_size
        offset = 0
        boxes = set()
        with path.open("rb") as handle:
            for _ in range(100_000):
                if offset == size:
                    return {b"ftyp", b"moov", b"mdat"}.issubset(boxes)
                if size - offset < 8:
                    return False
                handle.seek(offset)
                length, kind = struct.unpack(">I4s", handle.read(8))
                minimum = 8
                if length == 1:
                    if size - offset < 16:
                        return False
                    length = struct.unpack(">Q", handle.read(8))[0]
                    minimum = 16
                elif length == 0:
                    length = size - offset
                if length < minimum or offset + length > size:
                    return False
                if kind == b"mdat" and length == minimum:
                    return False
                boxes.add(kind)
                offset += length
        return False
    except (OSError, struct.error):
        return False


def fingerprint(files, settings):
    sources = []
    for raw in files:
        path = Path(raw)
        if not path.exists():
            sources.append({"name": path.name, "missing": True})
        elif path.suffix.lower() in {".mp4", ".m4v", ".mov"}:
            stat = path.stat()
            sources.append({"name": path.name, "size": stat.st_size, "mtime_ns": stat.st_mtime_ns})
        else:
            sources.append({"name": path.name, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
    value = {"schema": 1, "files": sources, "settings": settings}
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def cache_matches(path: Path, key: str):
    meta = read_json(path.with_suffix(path.suffix + ".cache.json"), {})
    if not isinstance(meta, dict) or meta.get("key") != key or not nonempty_text(path):
        return False
    return meta.get("sha256") == hashlib.sha256(path.read_bytes()).hexdigest()


def save_cached(path: Path, text: str, key: str):
    if not text or not text.strip():
        raise ValueError("AI returned empty output; the result has not been marked complete")
    if path.exists() and not cache_matches(path, key):
        atomic_bytes(path.with_name(path.stem + ".previous" + path.suffix), path.read_bytes())
    atomic_text(path, text)
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    atomic_text(path.with_suffix(path.suffix + ".cache.json"), json.dumps({"key": key, "schema": 1, "sha256": digest}))


def checked_ai_text(response, provider):
    if provider == "openai":
        if getattr(response, "status", "completed") != "completed":
            raise ValueError("AI response is incomplete. Check output-token limit, then retry.")
        result = getattr(response, "output_text", "") or ""
    else:
        if getattr(response, "stop_reason", None) in {"max_tokens", "refusal", "pause_turn"}:
            raise ValueError("AI response is incomplete or refused; no completed result was saved")
        result = "\n".join(getattr(part, "text", "") for part in getattr(response, "content", []))
    if not result.strip():
        raise ValueError("AI returned empty text; no completed result was saved")
    return result.strip()


def redact(text):
    text = str(text)
    for key in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"):
        value = os.getenv(key, "")
        if value:
            text = text.replace(value, "[REDACTED]")
    text = re.sub(r"\bsk-[A-Za-z0-9_-]{8,}", "[REDACTED]", text)
    # Keep error locations while removing signed URL parameters and user identifiers.
    return re.sub(r"(https?://[^\s?\#\"'<>]+)[?\#][^\s\"'<>]*", r"\1?[REDACTED]", text)
