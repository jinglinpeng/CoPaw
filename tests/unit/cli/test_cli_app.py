# -*- coding: utf-8 -*-
from click.testing import CliRunner

from qwenpaw.cli import app_cmd as app_cmd_module
from qwenpaw.config import utils as config_utils


def test_app_cmd_sets_runtime_api_without_persisting_last_api(monkeypatch):
    runtime_calls = []
    last_api_calls = []
    uvicorn_calls = []

    monkeypatch.setattr(
        app_cmd_module,
        "set_runtime_api",
        lambda host, port: runtime_calls.append((host, port)),
    )
    monkeypatch.setattr(
        app_cmd_module,
        "write_last_api",
        lambda host, port: last_api_calls.append((host, port)),
    )
    monkeypatch.setattr(
        app_cmd_module.uvicorn,
        "run",
        lambda *args, **kwargs: uvicorn_calls.append((args, kwargs)),
    )

    result = CliRunner().invoke(
        app_cmd_module.app_cmd,
        ["--host", "0.0.0.0", "--port", "19088", "--no-write-last-api"],
    )

    assert result.exit_code == 0
    assert runtime_calls == [("127.0.0.1", 19088)]
    assert not last_api_calls
    assert uvicorn_calls[0][1]["host"] == "0.0.0.0"
    assert uvicorn_calls[0][1]["port"] == 19088


def test_app_cmd_persists_last_api_by_default(monkeypatch):
    runtime_calls = []
    last_api_calls = []

    monkeypatch.setattr(
        app_cmd_module,
        "set_runtime_api",
        lambda host, port: runtime_calls.append((host, port)),
    )
    monkeypatch.setattr(
        app_cmd_module,
        "write_last_api",
        lambda host, port: last_api_calls.append((host, port)),
    )
    monkeypatch.setattr(
        app_cmd_module.uvicorn,
        "run",
        lambda *args, **kwargs: None,
    )

    result = CliRunner().invoke(
        app_cmd_module.app_cmd,
        ["--host", "127.0.0.1", "--port", "18088"],
    )

    assert result.exit_code == 0
    assert runtime_calls == [("127.0.0.1", 18088)]
    assert last_api_calls == [("127.0.0.1", 18088)]


def test_read_last_api_prefers_runtime_api(monkeypatch):
    monkeypatch.setenv(config_utils.RUNTIME_API_HOST_ENV, "127.0.0.1")
    monkeypatch.setenv(config_utils.RUNTIME_API_PORT_ENV, "19088")
    monkeypatch.setattr(
        config_utils,
        "load_config",
        lambda: (_ for _ in ()).throw(AssertionError("load_config called")),
    )

    assert config_utils.read_last_api() == ("127.0.0.1", 19088)
