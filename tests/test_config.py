from pathlib import Path

import yaml

from mu_dekhuchi.config import Settings, parse_env_text


def _mapping(**overrides: object) -> dict:
    data = {
        "server": {"host": "127.0.0.1", "port": 8080},
        "dvr": {"host": "192.168.0.100", "port": 554, "user": "admin", "password": "secret"},
        "capture": {"mode": "decrypt", "stream": "sub", "fps": 10, "quality": 5},
        "cameras": [
            {"id": "1", "name": "Front Door", "channel": 1},
            {"id": "2", "name": "Backyard", "channel": 2},
        ],
        "vision": {"backend": "mock"},
        "events": {"db": "data/events.db", "dir": "data/events"},
        "recording": {"dir": "recordings"},
    }
    data.update(overrides)
    return data


def test_cameras_and_urls_from_yaml() -> None:
    settings = Settings.from_mapping(
        _mapping(
            dvr={
                "host": "192.168.0.100",
                "port": 554,
                "user": "admin",
                "password": "p@ss&x",
            }
        ),
        root=Path("/tmp/mu_dekhuchi-config-test"),
    )

    assert [camera.id for camera in settings.cameras] == ["1", "2"]
    assert settings.cameras[0].name == "Front Door"
    assert settings.cameras[0].stream_id == "102"
    assert (
        settings.cameras[0].stream_url
        == "rtsp://admin:p%40ss%26x@192.168.0.100:554/Streaming/Channels/102"
    )
    assert settings.stream_mode == "decrypt"


def test_main_stream_is_default() -> None:
    settings = Settings.from_mapping(
        _mapping(capture={"mode": "decrypt"}),
        root=Path("/tmp/mu_dekhuchi-config-test"),
    )

    assert [camera.stream_id for camera in settings.cameras] == ["101", "201"]
    assert settings.stream_kind == "main"


def test_sub_stream_uses_channel_02() -> None:
    settings = Settings.from_mapping(
        _mapping(capture={"mode": "decrypt", "stream": "sub"}),
        root=Path("/tmp/mu_dekhuchi-config-test"),
    )

    assert [camera.stream_id for camera in settings.cameras] == ["102", "202"]
    assert settings.stream_kind == "sub"


def test_invalid_values_fall_back() -> None:
    settings = Settings.from_mapping(
        _mapping(
            capture={"mode": "banana", "stream": "nope"},
            vision={"backend": "banana"},
        ),
        root=Path("/tmp/mu_dekhuchi-config-test"),
    )

    assert settings.stream_mode == "rtsp"
    assert settings.stream_kind == "main"
    assert settings.vision_backend == "mock"


def test_load_expands_secrets_from_environment(tmp_path: Path) -> None:
    config = {
        "server": {"dashboard_password": "${DASHBOARD_PASSWORD}"},
        "dvr": {"host": "10.0.0.5", "user": "admin", "password": "${STREAM_PASS}"},
        "capture": {"mode": "decrypt"},
        "cameras": [{"id": "1", "name": "Test", "channel": 1}],
        "vision": {"backend": "openai", "api_key": "${VISION_API_KEY}"},
        "events": {"db": "data/events.db", "dir": "data/events"},
        "recording": {"dir": "recordings"},
    }
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(config))
    (tmp_path / ".env").write_text("STREAM_PASS=p@ss&x\n")

    settings = Settings.load(
        config_file=path,
        env={"DASHBOARD_PASSWORD": "dash", "VISION_API_KEY": "vision-key"},
        cwd=tmp_path,
    )

    assert settings.dashboard_password == "dash"
    assert settings.vision_api_key == "vision-key"
    assert "p%40ss%26x" in settings.cameras[0].stream_url
    assert settings.root == tmp_path


def test_parse_env_text_preserves_special_characters() -> None:
    env = parse_env_text("STREAM_PASS=p@ss&word$*\nDASHBOARD_PASSWORD='two words'\n")

    assert env["STREAM_PASS"] == "p@ss&word$*"
    assert env["DASHBOARD_PASSWORD"] == "two words"
