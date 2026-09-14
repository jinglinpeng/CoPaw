# -*- coding: utf-8 -*-
"""Regression tests for downloadable Docker verification builds."""

from pathlib import Path

import yaml


REPO_ROOT = Path(__file__).resolve().parents[3]


def _load_workflow(name: str) -> dict:
    return yaml.load(
        (REPO_ROOT / ".github/workflows" / name).read_text(
            encoding="utf-8",
        ),
        Loader=yaml.BaseLoader,
    )


def test_manual_docker_build_defaults_to_download_without_pushing() -> None:
    workflow = _load_workflow("docker-release.yml")
    push = workflow["on"]["workflow_dispatch"]["inputs"]["push_image"]
    assert push["type"] == "boolean"
    assert push["default"] == "false"

    verification = workflow["jobs"]["verify-docker"]["with"]
    build_only = (
        "github.event_name == 'workflow_dispatch' && !inputs.push_image"
    )
    assert verification["export_docker_image"] == f"${{{{ {build_only} }}}}"
    assert verification["docker_node_image"] == (
        f"${{{{ {build_only} && 'node:20-bookworm-slim' || '' }}}}"
    )
    assert verification["docker_uv_image"] == (
        f"${{{{ {build_only} && 'ghcr.io/astral-sh/uv:latest' || '' }}}}"
    )


def test_release_publication_remains_enabled() -> None:
    workflow = _load_workflow("docker-release.yml")
    assert workflow["on"]["release"]["types"] == ["published"]
    publication = workflow["jobs"]["build-and-push"]
    assert publication["if"] == (
        "${{ github.event_name == 'release' || inputs.push_image }}"
    )
    assert "--push" in publication["steps"][-1]["run"]
    assert publication["needs"] == ["verify-docker"]


def test_download_exports_verified_image_in_the_same_job() -> None:
    workflow = _load_workflow("release-verify.yml")
    inputs = workflow["on"]["workflow_call"]["inputs"]
    assert inputs["export_docker_image"]["default"] == "false"
    steps = workflow["jobs"]["verify-docker"]["steps"]
    by_name = {step.get("name"): step for step in steps}
    login = by_name["Log in to Aliyun ACR (when credentials available)"]
    assert login["if"] == (
        "env.ACR_USERNAME != '' && !inputs.export_docker_image"
    )
    export = by_name["Export verified Docker image"]
    upload = by_name["Upload verified Docker image"]
    assert export["if"] == upload["if"] == "inputs.export_docker_image"
    assert "docker save qwenpaw-verify:test | gzip" in export["run"]
    assert "--push" not in export["run"]
    assert upload["uses"] == "actions/upload-artifact@v4"
    assert upload["with"]["path"] == (
        "${{ runner.temp }}/qwenpaw-image.tar.gz"
    )
    assert upload["with"]["name"] == "qwenpaw-image-amd64"
    assert steps.index(by_name["Verify container version"]) < steps.index(
        export,
    )
    assert steps.index(export) < steps.index(upload)
    assert steps.index(upload) < steps.index(by_name["Stop container"])
