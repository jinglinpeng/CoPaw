# -*- coding: utf-8 -*-
"""Backup CLI behavior, including scheduler-safe failure exits."""

import json

from click.testing import CliRunner
import httpx
import pytest

from qwenpaw.cli import backup_cmd


def invoke(monkeypatch, handler, args=()):
    monkeypatch.setattr(
        backup_cmd,
        "client",
        lambda url: httpx.Client(
            base_url=f"{url}/api",
            transport=httpx.MockTransport(handler),
        ),
    )
    monkeypatch.setattr(backup_cmd.time, "sleep", lambda _: None)
    return CliRunner().invoke(
        backup_cmd.backup_group,
        ["create", *args],
        input="",
    )


def test_create_includes_all_agents_and_waits_for_completion(monkeypatch):
    requests = []

    def handler(request):
        requests.append(request)
        if request.url.path == "/api/agents":
            return httpx.Response(
                200,
                json={"agents": [{"id": "default"}, {"id": "other"}]},
            )
        if request.method == "POST":
            return httpx.Response(
                202,
                json={"job_id": "job1", "status": "pending"},
            )
        return httpx.Response(
            200,
            json={
                "job_id": "job1",
                "status": "completed",
                "result": {"id": "backup1"},
            },
        )

    result = invoke(monkeypatch, handler, ["--name", "Scheduled"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output) == {"id": "backup1"}
    payload = json.loads(requests[1].content)
    assert payload["agents"] == ["default", "other"]
    assert payload["scope"] == {
        "include_agents": True,
        "include_global_config": True,
        "include_secrets": False,
        "include_skill_pool": True,
    }
    assert requests[2].url.path == "/api/backups/jobs/job1"


def test_agent_selection_and_scope_options(monkeypatch):
    def handler(request):
        if request.method == "GET":
            return httpx.Response(
                200,
                json={"agents": [{"id": "chosen"}, {"id": "other"}]},
            )
        payload = json.loads(request.content)
        assert payload["agents"] == ["chosen"]
        assert payload["scope"] == {
            "include_agents": True,
            "include_global_config": False,
            "include_secrets": True,
            "include_skill_pool": False,
        }
        return httpx.Response(
            202,
            json={
                "job_id": "job1",
                "status": "completed",
                "result": {"id": "backup1"},
            },
        )

    result = invoke(
        monkeypatch,
        handler,
        [
            "--agent",
            "chosen",
            "--agent",
            "chosen",
            "--no-global-config",
            "--include-secrets",
            "--no-skill-pool",
        ],
    )
    assert result.exit_code == 0, result.output


def test_unknown_agent_fails_before_creating_job(monkeypatch):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={"agents": []})

    result = invoke(monkeypatch, handler, ["--agent", "missing"])
    assert result.exit_code == 1
    assert "Unknown agent IDs: missing" in result.output
    assert len(calls) == 1


def test_no_agents_skips_discovery_and_uses_timestamp(monkeypatch):
    def handler(request):
        assert request.method == "POST"
        payload = json.loads(request.content)
        assert payload["name"].startswith("Backup ")
        assert payload["agents"] == []
        assert payload["scope"]["include_agents"] is False
        return httpx.Response(
            202,
            json={"job_id": "j", "status": "completed", "result": {"id": "b"}},
        )

    result = invoke(monkeypatch, handler, ["--no-agents"])
    assert result.exit_code == 0, result.output


def test_conflicting_agent_options_are_usage_error(monkeypatch):
    result = invoke(
        monkeypatch,
        lambda _: pytest.fail("must not call API"),
        ["--no-agents", "--agent", "default"],
    )
    assert result.exit_code == 2
    assert "cannot be combined" in result.output


@pytest.mark.parametrize("status", ["failed", "cancelled"])
def test_failed_job_exits_nonzero(monkeypatch, status):
    result = invoke(
        monkeypatch,
        lambda _: httpx.Response(
            202,
            json={"job_id": "job1", "status": status, "error": "disk full"},
        ),
        ["--no-agents"],
    )
    assert result.exit_code == 1
    assert "disk full" in result.output


def test_wait_timeout_identifies_job_and_does_not_cancel(monkeypatch):
    clock = iter([0, 2])
    monkeypatch.setattr(backup_cmd.time, "monotonic", lambda: next(clock))
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(
            202,
            json={"job_id": "pending-job", "status": "pending"},
        )

    result = invoke(monkeypatch, handler, ["--no-agents", "--timeout", "1"])
    assert result.exit_code == 1
    assert "pending-job" in result.output
    assert "not been cancelled" in result.output
    assert len(calls) == 1


def test_http_conflict_exits_nonzero(monkeypatch):
    result = invoke(
        monkeypatch,
        lambda _: httpx.Response(
            409,
            json={"detail": "Backup operation already running"},
        ),
        ["--no-agents"],
    )
    assert result.exit_code == 1
    assert "409" in result.output


def test_connection_failure_is_actionable(monkeypatch):
    def handler(request):
        raise httpx.ConnectError("Connection refused", request=request)

    result = invoke(monkeypatch, handler, ["--no-agents"])
    assert result.exit_code == 1
    assert "Connection refused" in result.output
