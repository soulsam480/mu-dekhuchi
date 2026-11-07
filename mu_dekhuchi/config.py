from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit

import yaml

CONFIG_FILE = "config.yaml"
ENV_TOKEN = re.compile(r"\$\{([A-Z0-9_]+)\}")


@dataclass(frozen=True)
class CameraConfig:
    id: str
    channel: int
    name: str
    stream_id: str
    stream_url: str
    snapshot_url: str


@dataclass
class Settings:
    root: Path
    host: str = "127.0.0.1"
    port: int = 8080
    dashboard_user: str = "admin"
    dashboard_password: str = ""

    stream_mode: str = "rtsp"
    stream_kind: str = "main"
    stream_ip: str = ""
    stream_port: int = 554
    stream_user: str = ""
    stream_pass: str = ""
    stream_transport: str = "tcp"
    stream_fps: int = 10
    stream_quality: int = 5
    verification_code: str = ""
    cameras: list[CameraConfig] = field(default_factory=list)

    recordings_dir: Path = Path("recordings")
    events_db: Path = Path("data/events.db")
    events_dir: Path = Path("data/events")
    event_retention_days: int = 30

    vision_backend: str = "mock"
    vision_base_url: str = "http://127.0.0.1:8000/v1"
    vision_api_key: str = ""
    vision_model: str = "lfm2.5-vl-3b"
    vision_interval: float = 2.0
    vision_cooldown: int = 20
    vision_max_tokens: int = 320
    vision_min_confidence: float = 0.75
    vision_disable_reasoning: bool = True
    vision_scan_all_cameras: bool = False
    vision_scan_interval: float = 2.0
    event_webhook_url: str = ""

    ffmpeg_path: str = "ffmpeg"

    @property
    def active_camera_id(self) -> str | None:
        return self.cameras[0].id if self.cameras else None

    @classmethod
    def load(
        cls,
        config_file: Path | None = None,
        env: dict[str, str] | None = None,
        cwd: Path | None = None,
    ) -> Settings:
        root = Path(cwd) if cwd is not None else Path(__file__).resolve().parents[1]
        env_file = root / ".env"
        file_env = parse_env_text(env_file.read_text()) if env_file.exists() else {}
        merged = {**file_env, **(os.environ if env is None else env)}

        configured = config_file or (
            Path(merged["MU_DEKHUCHI_CONFIG"])
            if merged.get("MU_DEKHUCHI_CONFIG")
            else root / CONFIG_FILE
        )
        path = configured if configured.is_absolute() else root / configured
        if not path.exists():
            raise FileNotFoundError(f"Config file not found: {path}")

        data = yaml.safe_load(path.read_text()) or {}
        return cls.from_mapping(_expand(data, merged), root=root)

    @classmethod
    def from_mapping(cls, data: dict, root: Path | None = None) -> Settings:
        root = root or Path.cwd()
        server = data.get("server") or {}
        dvr = data.get("dvr") or {}
        capture = data.get("capture") or {}
        vision = data.get("vision") or {}
        events = data.get("events") or {}
        recording = data.get("recording") or {}

        stream_value = str(capture.get("stream", "main")).lower()
        stream_kind = "sub" if stream_value == "sub" else "main"
        suffix = "01" if stream_kind == "main" else "02"
        stream_ip = str(dvr.get("host") or "")
        stream_user = str(dvr.get("user") or "")
        stream_pass = str(dvr.get("password") or "")
        stream_port = _as_int(dvr.get("port"), 554, 1, 65535)

        cameras = [
            _camera(entry, suffix, stream_ip, stream_user, stream_pass, stream_port)
            for entry in (data.get("cameras") or [])
            if entry.get("channel")
        ]
        if not cameras:
            cameras = [
                _camera(
                    {"id": "1", "name": "Camera", "channel": 1},
                    suffix,
                    stream_ip,
                    stream_user,
                    stream_pass,
                    stream_port,
                )
            ]

        mode = str(capture.get("mode", "rtsp")).lower()
        backend = str(vision.get("backend", "mock")).lower()

        return cls(
            root=root,
            host=str(server.get("host") or "127.0.0.1"),
            port=_as_int(server.get("port"), 8080, 1, 65535),
            dashboard_user=str(server.get("dashboard_user") or "admin"),
            dashboard_password=str(server.get("dashboard_password") or ""),
            stream_mode=mode if mode in {"decrypt", "rtsp", "snapshot"} else "rtsp",
            stream_kind=stream_kind,
            stream_ip=stream_ip,
            stream_port=stream_port,
            stream_user=stream_user,
            stream_pass=stream_pass,
            stream_transport=(
                "udp" if str(capture.get("transport", "tcp")).lower() == "udp" else "tcp"
            ),
            stream_fps=_as_int(capture.get("fps"), 10, 1, 30),
            stream_quality=_as_int(capture.get("quality"), 5, 2, 31),
            verification_code=str(capture.get("verification_code") or ""),
            cameras=cameras,
            recordings_dir=_path(root, recording.get("dir"), "recordings"),
            events_db=_path(root, events.get("db"), "data/events.db"),
            events_dir=_path(root, events.get("dir"), "data/events"),
            event_retention_days=_as_int(events.get("retention_days"), 30, 1, 3650),
            vision_backend=backend if backend in {"mock", "openai"} else "mock",
            vision_base_url=str(vision.get("base_url") or "http://127.0.0.1:8000/v1"),
            vision_api_key=str(vision.get("api_key") or ""),
            vision_model=str(vision.get("model") or "lfm2.5-vl-3b"),
            vision_interval=_as_float(vision.get("interval"), 2.0, 0.5, 60.0),
            vision_cooldown=_as_int(vision.get("cooldown"), 20, 1, 3600),
            vision_max_tokens=_as_int(vision.get("max_tokens"), 320, 64, 4096),
            vision_min_confidence=_as_float(vision.get("min_confidence"), 0.75, 0.0, 1.0),
            vision_disable_reasoning=_as_bool(vision.get("disable_reasoning"), True),
            vision_scan_all_cameras=_as_bool(vision.get("scan_all_cameras"), False),
            vision_scan_interval=_as_float(vision.get("scan_interval"), 2.0, 0.5, 30.0),
            event_webhook_url=str(vision.get("webhook_url") or ""),
            ffmpeg_path=str(data.get("ffmpeg") or "ffmpeg"),
        )


def _camera(
    entry: dict,
    suffix: str,
    host: str,
    user: str,
    password: str,
    port: int,
) -> CameraConfig:
    channel = _as_int(entry.get("channel"), 1, 1, 99)
    stream_id = str(entry.get("stream_id") or f"{channel}{suffix}")
    stream_path = str(entry.get("stream_path") or f"/Streaming/Channels/{stream_id}")
    return CameraConfig(
        id=str(entry.get("id") or channel),
        channel=channel,
        name=str(entry.get("name") or f"Camera {channel}"),
        stream_id=stream_id,
        stream_url=_build_rtsp_url(host, user, password, port, stream_path),
        snapshot_url=f"http://{host}/ISAPI/Streaming/channels/{stream_id}/picture",
    )


def _build_rtsp_url(host: str, user: str, password: str, port: int, stream_path: str) -> str:
    auth = f"{quote(user, safe='')}:{quote(password, safe='')}@" if user else ""
    path = stream_path if stream_path.startswith("/") else f"/{stream_path}"
    return f"rtsp://{auth}{host}:{port}{path}"


def _expand(value: object, env: dict[str, str]) -> Any:
    if isinstance(value, str):
        return ENV_TOKEN.sub(lambda match: env.get(match.group(1), ""), value)
    if isinstance(value, dict):
        return {key: _expand(item, env) for key, item in value.items()}
    if isinstance(value, list):
        return [_expand(item, env) for item in value]
    return value


def _path(root: Path, value: object, fallback: str) -> Path:
    return root / (str(value) if value else fallback)


def _as_int(value: object, default: int, minimum: int, maximum: int) -> int:
    if not isinstance(value, (int, float, str)):
        return default
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, parsed))


def _as_float(value: object, default: float, minimum: float, maximum: float) -> float:
    if not isinstance(value, (int, float, str)):
        return default
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, parsed))


def _as_bool(value: object, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() not in {"0", "false", "no", "off"}


def parse_env_text(text: str) -> dict[str, str]:
    """Parse dotenv syntax, preserving special characters in secrets."""
    values: dict[str, str] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        values[key.strip()] = value
    return values


def redact_url(value: str) -> str:
    try:
        parts = urlsplit(value)
        if parts.username or parts.password:
            host = parts.hostname or ""
            port = f":{parts.port}" if parts.port else ""
            return parts._replace(netloc=f"***:***@{host}{port}").geturl()
        return value
    except ValueError:
        return "rtsp://<camera>"
