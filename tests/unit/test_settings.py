"""Settings resolve across defaults, config file, environment and overrides."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scopiengine.errors import ConfigurationError
from scopiengine.settings import DEFAULT_STORAGE_DSN, Settings, load_settings


def test_defaults_apply_when_nothing_is_set() -> None:
    settings = load_settings(env={})
    assert settings.storage == DEFAULT_STORAGE_DSN
    assert settings.port == 9500
    assert settings.plugins == ()


def test_environment_overrides_defaults() -> None:
    settings = load_settings(env={"SCOPI_PORT": "9601", "SCOPI_STORAGE": "sqlite:///./a.db"})
    assert settings.port == 9601
    assert settings.storage == "sqlite:///./a.db"


def test_overrides_beat_environment() -> None:
    settings = load_settings(env={"SCOPI_PORT": "9601"}, port=9700)
    assert settings.port == 9700


def test_none_overrides_are_ignored() -> None:
    settings = load_settings(env={"SCOPI_PORT": "9601"}, port=None)
    assert settings.port == 9601


def test_config_file_is_read_and_beaten_by_environment(tmp_path: Path) -> None:
    config = tmp_path / "scopi.json"
    config.write_text(json.dumps({"port": 9001, "log_level": "DEBUG"}), encoding="utf-8")
    settings = load_settings(config_path=config, env={"SCOPI_PORT": "9002"})
    assert settings.port == 9002
    assert settings.log_level == "DEBUG"


def test_plugins_parse_as_a_comma_separated_list() -> None:
    settings = load_settings(env={"SCOPI_PLUGINS": "pkg.one, pkg.two ,"})
    assert settings.plugins == ("pkg.one", "pkg.two")


@pytest.mark.parametrize("raw,expected", [("true", True), ("0", False), ("on", True)])
def test_booleans_parse_from_common_spellings(raw: str, expected: bool) -> None:
    assert load_settings(env={"SCOPI_STRICT_PLUGINS": raw}).strict_plugins is expected


def test_bad_boolean_is_rejected_with_the_variable_name() -> None:
    with pytest.raises(ConfigurationError, match="SCOPI_STRICT_PLUGINS"):
        load_settings(env={"SCOPI_STRICT_PLUGINS": "maybe"})


def test_bad_integer_is_rejected_with_the_variable_name() -> None:
    with pytest.raises(ConfigurationError, match="SCOPI_PORT"):
        load_settings(env={"SCOPI_PORT": "nine thousand"})


def test_unknown_config_key_is_rejected(tmp_path: Path) -> None:
    config = tmp_path / "scopi.json"
    config.write_text(json.dumps({"prot": 9001}), encoding="utf-8")
    with pytest.raises(ConfigurationError, match="unknown setting 'prot'"):
        load_settings(config_path=config, env={})


def test_missing_config_file_is_reported(tmp_path: Path) -> None:
    with pytest.raises(ConfigurationError, match="cannot read config file"):
        load_settings(config_path=tmp_path / "absent.json", env={})


def test_malformed_config_file_is_reported(tmp_path: Path) -> None:
    config = tmp_path / "scopi.json"
    config.write_text("{not json", encoding="utf-8")
    with pytest.raises(ConfigurationError, match="not valid JSON"):
        load_settings(config_path=config, env={})


@pytest.mark.parametrize(
    "kwargs,message",
    [
        ({"port": 0}, "port must be between"),
        ({"port": 70000}, "port must be between"),
        ({"storage": ""}, "storage DSN must not be empty"),
        ({"log_format": "xml"}, "log_format must be"),
        ({"batch_size": 0}, "batch_size must be >= 1"),
        ({"flush_interval": 0.0}, "flush_interval must be > 0"),
    ],
)
def test_invalid_values_are_rejected(kwargs: dict[str, object], message: str) -> None:
    with pytest.raises(ConfigurationError, match=message):
        Settings(**kwargs)  # type: ignore[arg-type]


def test_to_dict_is_json_serialisable() -> None:
    payload = load_settings(env={}).to_dict()
    assert json.loads(json.dumps(payload))["port"] == 9500
    assert payload["plugins"] == []
