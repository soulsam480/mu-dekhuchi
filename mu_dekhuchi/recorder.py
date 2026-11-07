from __future__ import annotations

import builtins
import contextlib
import re
import subprocess
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import IO, Any

NAME_PATTERN = re.compile(r"^rec-\d{8}-\d{6}\.mp4$")
START_CODE = b"\x00\x00\x00\x01"
STOP_TIMEOUT = 5.0


class NalRecorder:
    """Pipe decrypted NAL units into ffmpeg and manage the saved MP4s."""

    def __init__(self, directory: Path, ffmpeg_path: str = "ffmpeg", codec: str = "h264") -> None:
        self.directory = Path(directory)
        self.ffmpeg_path = ffmpeg_path
        self.codec = codec

        self._lock = threading.Lock()
        self._proc: subprocess.Popen[bytes] | None = None
        self._name: str | None = None
        self._started_at: datetime | None = None
        self._bytes = 0
        self._error: str | None = None

    def start(self) -> dict:
        """Spawn ffmpeg and begin recording into a timestamped MP4."""
        with self._lock:
            if self._proc is not None:
                raise RuntimeError("Recording already in progress")

            self.directory.mkdir(parents=True, exist_ok=True)
            started_at = datetime.now().astimezone()
            name = f"rec-{started_at:%Y%m%d-%H%M%S}.mp4"

            proc = subprocess.Popen(
                [self.ffmpeg_path, *_ffmpeg_args(self.directory / name, self.codec)],
                stdin=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            self._proc = proc
            self._name = name
            self._started_at = started_at
            self._bytes = 0
            self._error = None
            return self._snapshot()

    def push(self, nal: bytes) -> None:
        """Write one Annex-B framed NAL unit; silently drop it when not recording."""
        with self._lock:
            proc = self._proc
            if proc is None or proc.stdin is None or proc.stdin.closed:
                return
            try:
                proc.stdin.write(START_CODE + nal)
                proc.stdin.flush()
            except (OSError, ValueError):
                return

    def stop(self) -> dict:
        """Close ffmpeg's stdin, wait for it to exit (killing after the timeout)."""
        with self._lock:
            proc = self._proc
            if proc is None:
                return self._snapshot()
            self._proc = None
            stdin = proc.stdin
            name = self._name

        _close_stdin(stdin)
        error = _await_exit(proc)
        path = self.directory / name if name is not None else None

        with self._lock:
            if path is not None:
                self._bytes = _file_size(path, self._bytes)
            if error:
                self._error = error
            return self._snapshot()

    def status(self) -> dict[str, Any]:
        with self._lock:
            return self._snapshot()

    def list(self) -> builtins.list[dict[str, Any]]:
        """Return saved recordings, newest first."""
        self.directory.mkdir(parents=True, exist_ok=True)
        entries: list[tuple[float, dict]] = []
        for path in self.directory.iterdir():
            if not path.is_file() or not NAME_PATTERN.fullmatch(path.name):
                continue
            try:
                stat = path.stat()
            except OSError:
                continue
            entries.append(
                (
                    stat.st_mtime,
                    {
                        "name": path.name,
                        "bytes": stat.st_size,
                        "modified_at": _iso_timestamp(stat.st_mtime),
                    },
                )
            )
        entries.sort(key=lambda entry: entry[0], reverse=True)
        return [item for _, item in entries]

    def resolve(self, name: str) -> Path | None:
        """Map a recording name to its path; reject anything but a plain MP4 name."""
        if not NAME_PATTERN.fullmatch(name):
            return None
        path = self.directory / name
        return path if path.parent == self.directory else None

    def remove(self, name: str) -> None:
        with self._lock:
            active = self._proc is not None and name == self._name
        if active:
            raise ValueError("Cannot delete a recording in progress")

        path = self.resolve(name)
        if path is None:
            raise ValueError(f"Invalid recording name: {name}")
        path.unlink()

    def _snapshot(self) -> dict:
        if self._proc is not None and self._name is not None:
            self._bytes = _file_size(self.directory / self._name, self._bytes)
        return {
            "recording": self._proc is not None,
            "name": self._name,
            "bytes": self._bytes,
            "started_at": self._started_at.isoformat() if self._started_at else None,
            "error": self._error,
        }


def _ffmpeg_args(path: Path, codec: str = "h264") -> list[str]:
    fmt = "hevc" if codec == "hevc" else "h264"
    return [
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-f",
        fmt,
        "-i",
        "pipe:0",
        "-c",
        "copy",
        "-movflags",
        "+faststart",
        "-f",
        "mp4",
        str(path),
    ]


def _close_stdin(stdin: IO[bytes] | None) -> None:
    if stdin is None or stdin.closed:
        return
    with contextlib.suppress(OSError):
        stdin.close()


def _await_exit(proc: subprocess.Popen[bytes]) -> str | None:
    try:
        proc.wait(timeout=STOP_TIMEOUT)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
        return "ffmpeg did not exit in time"

    message = _last_stderr_line(proc)
    if proc.returncode:
        return message or f"ffmpeg exited with code {proc.returncode}"
    return message or None


def _last_stderr_line(proc: subprocess.Popen[bytes]) -> str:
    stream = proc.stderr
    if stream is None or stream.closed:
        return ""
    try:
        text = stream.read()
    except (OSError, ValueError):
        return ""
    if isinstance(text, bytes):
        text = text.decode("utf-8", "replace")
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return lines[-1] if lines else ""


def _file_size(path: Path, fallback: int = 0) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return fallback


def _iso_timestamp(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, tz=UTC).isoformat()
