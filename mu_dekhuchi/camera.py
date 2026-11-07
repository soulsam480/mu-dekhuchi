from __future__ import annotations

import re
import subprocess
import threading
import time
from collections import deque
from collections.abc import Callable
from contextlib import suppress
from io import BytesIO
from typing import Any

from PIL import Image

from .config import CameraConfig
from .crypto import HikDecryptor
from .rtsp import RtspAuthError, RtspClient

JPEG_START = b"\xff\xd8\xff"
JPEG_END = b"\xff\xd9"
FPS_WINDOW_SECONDS = 5.0
RECONNECT_SECONDS = 3.0
AUTH_BACKOFF_SECONDS = 300.0


class CameraError(RuntimeError):
    """A camera pipeline failure that should trigger a reconnect."""


def split_jpegs(buffer: bytes | bytearray) -> tuple[list[bytes], bytes]:
    """Split a byte stream into complete JPEG images and a trailing partial."""
    frames: list[bytes] = []
    buf = bytearray(buffer) if isinstance(buffer, bytes) else buffer

    while True:
        start = buf.find(JPEG_START)
        if start == -1:
            buf.clear()
            return frames, b""
        if start:
            del buf[:start]

        end = buf.find(JPEG_END, 2)
        if end == -1:
            return frames, bytes(buf)

        frames.append(bytes(buf[: end + 2]))
        del buf[: end + 2]


def _sanitize(message: str) -> str:
    return re.sub(r"rtsp://\S+", "rtsp://<camera>", message).strip()


class CameraWorker:
    """One RTSP camera: decrypt NALs, decode with ffmpeg, expose JPEG frames."""

    def __init__(
        self,
        camera: CameraConfig,
        verification_code: str,
        *,
        code_provider: Callable[[], str] | None = None,
        ffmpeg_path: str = "ffmpeg",
        fps: int = 10,
        quality: int = 5,
        recorder: Any = None,
    ) -> None:
        self.camera = camera
        self.verification_code = verification_code
        self.code_provider = code_provider
        self.ffmpeg_path = ffmpeg_path
        self.fps = fps
        self.quality = quality
        self.recorder = recorder

        self.on_nal: Callable[[bytes], None] | None = None

        self._lock = threading.RLock()
        self._condition = threading.Condition(self._lock)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._client: RtspClient | None = None
        self._ffmpeg: subprocess.Popen[bytes] | None = None
        self._frame_thread: threading.Thread | None = None
        self._stderr_thread: threading.Thread | None = None

        self._latest = b""
        self._sequence = 0
        self._frames = 0
        self._width: int | None = None
        self._height: int | None = None
        self._frame_times: deque[float] = deque(maxlen=300)
        self._byte_window: deque[tuple[float, int]] = deque()
        self._last_frame_at: float | None = None
        self._live_since: float | None = None
        self._restarts = 0
        self._viewers = 0

        self._state = "idle"
        self._error = ""
        self._auth_failed = False
        self._ffmpeg_error = ""
        self._codec = "h264"
        self._decryptor = HikDecryptor(verification_code, header_size=1)
        self._decrypted_nals = 0
        self._flagged_nals = 0

    def start(self) -> None:
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            self._stop.clear()
            self._state = "connecting"
            self._thread = threading.Thread(
                target=self._run, name=f"camera-{self.camera.id}", daemon=True
            )
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._close_client()
        self._close_ffmpeg()
        with self._lock:
            self._state = "stopped"
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5.0)

    def latest(self) -> tuple[bytes, int]:
        with self._lock:
            return self._latest, self._sequence

    def add_viewer(self) -> None:
        with self._lock:
            self._viewers += 1

    def remove_viewer(self) -> None:
        with self._lock:
            self._viewers = max(0, self._viewers - 1)

    def snapshot(self, timeout: float = 8.0) -> bytes:
        with self._condition:
            target = self._sequence
            self._condition.wait_for(
                lambda: self._sequence != target and bool(self._latest), timeout
            )
            if not self._latest:
                raise TimeoutError("Timed out waiting for a camera frame")
            return self._latest

    def status(self) -> dict[str, Any]:
        with self._lock:
            now = time.monotonic()
            recent = [stamp for stamp in self._frame_times if now - stamp <= FPS_WINDOW_SECONDS]
            elapsed = max(0.5, min(FPS_WINDOW_SECONDS, now - (self._live_since or now)))
            fps = len(recent) / elapsed if recent else 0.0
            bitrate = (
                sum(size for stamp, size in self._byte_window if now - stamp <= 5.0) * 8 / 5 / 1000
            )
            return {
                "name": self.camera.name,
                "state": self._state,
                "live": self._state == "live",
                "error": self._error or None,
                "authFailed": self._auth_failed,
                "width": self._width,
                "height": self._height,
                "fps": round(fps, 1),
                "bitrateKbps": round(bitrate),
                "viewers": self._viewers,
                "frames": self._frames,
                "uptimeSeconds": int(now - self._live_since) if self._live_since else 0,
                "lastFrameAt": int(self._last_frame_at * 1000) if self._last_frame_at else None,
                "restarts": self._restarts,
                "transport": "rtsp+aes",
                "decryption": {
                    "enabled": self._decryptor.enabled,
                    "codec": self._codec,
                    "decryptedNals": self._decrypted_nals,
                    "flaggedNals": self._flagged_nals,
                },
            }

    def _run(self) -> None:
        backoff = RECONNECT_SECONDS
        while not self._stop.is_set():
            try:
                self._run_session()
                if not self._stop.is_set():
                    raise CameraError("RTSP stream ended")
            except RtspAuthError as error:
                backoff = AUTH_BACKOFF_SECONDS
                self._fail(error, auth_failed=True)
            except Exception as error:
                backoff = RECONNECT_SECONDS
                self._fail(error)
            finally:
                self._close_client()
                self._close_ffmpeg()
            if self._stop.wait(backoff):
                break

    def _header_size(self) -> int:
        return 2 if self._codec == "hevc" else 1

    def _refresh_code(self) -> None:
        if self.verification_code or self.code_provider is None:
            return
        try:
            code = self.code_provider()
        except Exception:
            return
        if code:
            self.verification_code = code
            with self._lock:
                self._decryptor = HikDecryptor(code, header_size=self._header_size())

    def _run_session(self) -> None:
        self._refresh_code()
        with self._lock:
            self._state = "connecting"
            self._error = ""
            self._auth_failed = False
            self._decryptor = HikDecryptor(self.verification_code, header_size=self._header_size())
            self._restarts += 1

        client = RtspClient(
            self.camera.stream_url,
            username="",
            password="",
            timeout=15.0,
        )
        self._client = client
        info = client.start()

        if info.codec not in {"h264", "hevc"}:
            raise CameraError(f"Unsupported codec: {info.codec}")
        with self._lock:
            self._codec = info.codec
            self._decryptor = HikDecryptor(self.verification_code, header_size=self._header_size())
        if self.recorder is not None:
            with suppress(Exception):
                self.recorder.codec = info.codec

        self._start_ffmpeg(info.codec)
        for parameter_set in (info.vps, info.sps, info.pps):
            if parameter_set:
                self._write_nal(parameter_set)

        client.run(self._on_rtp_nal, self._stop)

    def _on_rtp_nal(self, nal: bytes, timestamp: int, flagged: bool) -> None:
        with self._lock:
            if flagged:
                self._flagged_nals += 1
            plain = self._decryptor.process(nal, flagged)
            if plain is not nal:
                self._decrypted_nals += 1

        self._write_nal(plain)
        if self.on_nal:
            self.on_nal(plain)

    def _start_ffmpeg(self, codec: str) -> None:
        self._close_ffmpeg()
        command = [
            self.ffmpeg_path,
            "-hide_banner",
            "-loglevel",
            "error",
            "-fflags",
            "+genpts",
            "-analyzeduration",
            "1000000",
            "-probesize",
            "500000",
            "-f",
            codec,
            "-i",
            "pipe:0",
            "-an",
        ]
        # Hikvision H.265 main streams often decode to 960x1088 (16-aligned CTU padding);
        # crop common padding to the intended display height.
        if codec == "hevc":
            command.extend(
                ["-vf", "crop=iw:if(eq(ih\\,1088)\\,1080\\,if(eq(ih\\,544)\\,540\\,ih)):0:0"]
            )
        command.extend(
            [
                "-q:v",
                str(self.quality),
                "-f",
                "image2pipe",
                "-vcodec",
                "mjpeg",
                "pipe:1",
            ]
        )
        process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
        )
        self._ffmpeg = process

        self._frame_thread = threading.Thread(
            target=self._read_frames, args=(process,), name=f"frames-{self.camera.id}", daemon=True
        )
        self._stderr_thread = threading.Thread(
            target=self._read_stderr,
            args=(process,),
            name=f"ffmpeg-err-{self.camera.id}",
            daemon=True,
        )
        self._frame_thread.start()
        self._stderr_thread.start()

    def _read_frames(self, process: subprocess.Popen[bytes]) -> None:
        buffer = bytearray()
        while not self._stop.is_set() and process.stdout:
            chunk = process.stdout.read(65536)
            if not chunk:
                break
            buffer.extend(chunk)
            frames, _ = split_jpegs(buffer)
            for frame in frames:
                self._publish(frame)

    def _read_stderr(self, process: subprocess.Popen[bytes]) -> None:
        while process.stderr:
            line = process.stderr.readline()
            if not line:
                break
            self._ffmpeg_error = _sanitize(line.decode("utf-8", "replace"))[-400:]

    def _publish(self, frame: bytes) -> None:
        now = time.monotonic()
        with self._condition:
            self._latest = frame
            self._sequence += 1
            self._frames += 1
            self._last_frame_at = now
            self._live_since = self._live_since or now
            self._frame_times.append(now)
            self._byte_window.append((now, len(frame)))
            while self._byte_window and now - self._byte_window[0][0] > 5.0:
                self._byte_window.popleft()
            if self._width is None:
                self._read_dimensions(frame)
            self._state = "live"
            self._error = ""
            self._condition.notify_all()

    def _read_dimensions(self, frame: bytes) -> None:
        try:
            with Image.open(BytesIO(frame)) as image:
                self._width, self._height = image.size
        except OSError:
            self._width = self._height = None

    def _write_nal(self, nal: bytes) -> None:
        process = self._ffmpeg
        if process is None or process.stdin is None:
            return
        with suppress(BrokenPipeError, ValueError):
            process.stdin.write(b"\x00\x00\x00\x01" + nal)

    def _fail(self, error: Exception, *, auth_failed: bool = False) -> None:
        with self._lock:
            self._state = "error" if auth_failed else "offline"
            self._error = _sanitize(str(error)) or self._ffmpeg_error or "Camera stream failed"
            self._auth_failed = auth_failed
            self._live_since = None

    def _close_client(self) -> None:
        client, self._client = self._client, None
        if client:
            with suppress(OSError):
                client.stop()

    def _close_ffmpeg(self) -> None:
        process, self._ffmpeg = self._ffmpeg, None
        if not process:
            return
        with suppress(OSError):
            if process.stdin:
                process.stdin.close()
        if process.poll() is None:
            process.kill()
        with suppress(subprocess.TimeoutExpired):
            process.wait(timeout=3)
