from __future__ import annotations

import builtins
import threading
from typing import Any

from .camera import CameraWorker
from .config import CameraConfig, Settings
from .isapi import read_verification_code
from .recorder import NalRecorder


class UnknownCameraError(KeyError):
    """The requested camera id is not configured."""


class CameraHub:
    """Owns the configured cameras and their recorders; one camera runs at a time."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._definitions: dict[str, CameraConfig] = {
            camera.id: camera for camera in settings.cameras
        }
        self.active_id: str | None = settings.active_camera_id
        self._cameras: dict[str, CameraWorker] = {}
        self._recorders: dict[str, NalRecorder] = {}
        self._lock = threading.RLock()

    def list(self) -> builtins.list[dict[str, object]]:
        return [
            {
                "id": camera.id,
                "name": camera.name,
                "channel": camera.channel,
                "active": camera.id == self.active_id,
            }
            for camera in self._definitions.values()
        ]

    def _fresh_verification_code(self) -> str:
        if self.settings.stream_mode != "decrypt":
            return ""
        if not self.settings.verification_code:
            code = read_verification_code(
                f"http://{self.settings.stream_ip}",
                self.settings.stream_user,
                self.settings.stream_pass,
            )
            if code:
                self.settings.verification_code = code
        return self.settings.verification_code

    def camera_for(self, camera_id: str) -> CameraWorker:
        key = str(camera_id)
        if key not in self._definitions:
            raise UnknownCameraError(key)
        with self._lock:
            if key not in self._cameras:
                recorder = self.recorder_for(key)
                worker = CameraWorker(
                    self._definitions[key],
                    self.settings.verification_code,
                    code_provider=self._fresh_verification_code,
                    ffmpeg_path=self.settings.ffmpeg_path,
                    fps=self.settings.stream_fps,
                    quality=self.settings.stream_quality,
                    recorder=recorder,
                )
                worker.on_nal = recorder.push
                self._cameras[key] = worker
            return self._cameras[key]

    def recorder_for(self, camera_id: str) -> NalRecorder:
        key = str(camera_id)
        if key not in self._definitions:
            raise UnknownCameraError(key)
        with self._lock:
            if key not in self._recorders:
                self._recorders[key] = NalRecorder(
                    self.settings.recordings_dir / key,
                    ffmpeg_path=self.settings.ffmpeg_path,
                )
            return self._recorders[key]

    @property
    def active_camera(self) -> CameraWorker | None:
        return self.camera_for(self.active_id) if self.active_id else None

    @property
    def active_recorder(self) -> NalRecorder | None:
        return self.recorder_for(self.active_id) if self.active_id else None

    def activate(self, camera_id: str) -> CameraWorker:
        key = str(camera_id)
        if key not in self._definitions:
            raise UnknownCameraError(key)

        with self._lock:
            if key == self.active_id and key in self._cameras:
                self._cameras[key].start()
                return self._cameras[key]

            previous_id = self.active_id
            if previous_id:
                previous_recorder = self._recorders.get(previous_id)
                if previous_recorder and previous_recorder.status()["recording"]:
                    previous_recorder.stop()
                previous_camera = self._cameras.get(previous_id)
                if previous_camera:
                    previous_camera.stop()

            self.active_id = key
            camera = self.camera_for(key)
            camera.start()
            return camera

    def status_payload(self) -> dict[str, Any]:
        camera = self.active_camera
        recorder = self.active_recorder
        return {
            "activeId": self.active_id,
            "cameras": self.list(),
            "camera": camera.status() if camera else _idle_camera(self.settings),
            "recorder": _recorder_payload(recorder) if recorder else _idle_recorder(),
        }

    def shutdown(self) -> None:
        with self._lock:
            for recorder in self._recorders.values():
                if recorder.status()["recording"]:
                    recorder.stop()
            for camera in self._cameras.values():
                camera.stop()


def _idle_camera(settings: Settings) -> dict[str, object]:
    return {
        "name": settings.cameras[0].name if settings.cameras else "Camera",
        "state": "idle",
        "live": False,
        "error": None,
        "authFailed": False,
        "width": None,
        "height": None,
        "fps": 0,
        "bitrateKbps": 0,
        "viewers": 0,
        "frames": 0,
        "uptimeSeconds": 0,
        "lastFrameAt": None,
        "restarts": 0,
        "transport": "rtsp+aes",
        "decryption": {"enabled": False, "codec": settings.stream_kind if hasattr(settings, "stream_kind") else "h264", "decryptedNals": 0, "flaggedNals": 0},
    }


def _recorder_payload(recorder: NalRecorder) -> dict[str, object]:
    status = recorder.status()
    return {
        "recording": bool(status.get("recording")),
        "name": status.get("name"),
        "startedAt": status.get("started_at"),
        "durationSeconds": status.get("duration_seconds", 0),
        "bytes": status.get("bytes", 0),
        "error": status.get("error"),
    }


def _idle_recorder() -> dict[str, object]:
    return {
        "recording": False,
        "name": None,
        "startedAt": None,
        "durationSeconds": 0,
        "bytes": 0,
        "error": None,
    }
