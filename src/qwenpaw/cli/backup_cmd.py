# -*- coding: utf-8 -*-
"""Non-interactive backup creation for system schedulers."""

from __future__ import annotations

from datetime import datetime
import time

import click
import httpx

from .http import client, print_json, resolve_base_url


@click.group("backup")
def backup_group() -> None:
    """Create backups through a running QwenPaw server."""


@backup_group.command("create")
@click.option(
    "--name",
    default=None,
    help="Backup name (defaults to a timestamp).",
)
@click.option("--description", default="", help="Optional backup description.")
@click.option(
    "--agent",
    "agents",
    multiple=True,
    help="Agent ID; repeat to select agents. Defaults to all agents.",
)
@click.option("--no-agents", is_flag=True, help="Exclude agent workspaces.")
@click.option(
    "--no-global-config",
    is_flag=True,
    help="Exclude global configuration.",
)
@click.option(
    "--include-secrets",
    is_flag=True,
    help="Include secrets (excluded by default).",
)
@click.option(
    "--no-skill-pool",
    is_flag=True,
    help="Exclude the shared skill pool.",
)
@click.option(
    "--base-url",
    default=None,
    help="Override the API URL; otherwise use global --host/--port.",
)
@click.option(
    "--timeout",
    type=click.FloatRange(min=0, min_open=True),
    default=3600.0,
    show_default=True,
    help="Maximum seconds to wait for completion; a timeout does not cancel the server job.",
)
@click.pass_context
def create_backup(
    ctx: click.Context,
    name: str | None,
    description: str,
    agents: tuple[str, ...],
    no_agents: bool,
    no_global_config: bool,
    include_secrets: bool,
    no_skill_pool: bool,
    base_url: str | None,
    timeout: float,
) -> None:
    """Create a backup and wait for completion. Print its metadata as JSON.

    Suitable for cron, launchd or Task Scheduler; no prompts are shown.
    The archive is stored in the server's configured backup directory.
    """
    if no_agents and agents:
        raise click.UsageError("--agent cannot be combined with --no-agents")
    payload = {
        "name": name
        or datetime.now().astimezone().strftime("Backup %Y-%m-%d %H:%M:%S"),
        "description": description,
        "scope": {
            "include_agents": not no_agents,
            "include_global_config": not no_global_config,
            "include_secrets": include_secrets,
            "include_skill_pool": not no_skill_pool,
        },
        "agents": [],
    }
    try:
        with client(resolve_base_url(ctx, base_url)) as api:
            if not no_agents:
                response = api.get("/agents")
                response.raise_for_status()
                available = [
                    agent["id"] for agent in response.json()["agents"]
                ]
                missing = set(agents) - set(available)
                if missing:
                    raise click.ClickException(
                        f"Unknown agent IDs: {', '.join(sorted(missing))}",
                    )
                payload["agents"] = (
                    list(dict.fromkeys(agents)) if agents else available
                )
            response = api.post("/backups/jobs", json=payload)
            response.raise_for_status()
            snapshot = response.json()
            job_id = snapshot["job_id"]
            deadline = time.monotonic() + timeout
            while True:
                status = snapshot["status"]
                if status == "completed":
                    print_json(snapshot["result"])
                    return
                if status in {"failed", "cancelled"}:
                    raise click.ClickException(
                        f"Backup job {job_id} {status}: {snapshot.get('error') or status}",
                    )
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise click.ClickException(
                        f"Timed out waiting for backup job {job_id}; the server job has not been cancelled.",
                    )
                time.sleep(min(1.0, remaining))
                response = api.get(f"/backups/jobs/{job_id}")
                response.raise_for_status()
                snapshot = response.json()
    except httpx.HTTPError as exc:
        raise click.ClickException(f"Backup request failed: {exc}") from exc
