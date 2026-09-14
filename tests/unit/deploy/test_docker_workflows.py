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
    build_only = "github.event_name == 'workflow_dispatch'"
    build_only += " && !inputs.push_image"
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
        "${{ !inputs.full_artifacts && inputs.artifact_run_id == '' && "
        "(github.event_name == 'release' || inputs.push_image) }}"
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
    condition = "env.ACR_USERNAME != ''" " && !inputs.export_docker_image"
    assert login["if"] == condition
    export = by_name["Export verified Docker image"]
    upload = by_name["Upload verified Docker image"]
    assert export["if"] == upload["if"] == "inputs.export_docker_image"
    assert "docker save qwenpaw-verify:test | gzip" in export["run"]
    assert "--push" not in export["run"]
    assert upload["uses"] == "actions/upload-artifact@v4"
    archive_path = "${{ runner.temp }}/qwenpaw-image.tar.gz"
    assert upload["with"]["path"] == archive_path
    assert upload["with"]["name"] == "qwenpaw-image-amd64"
    assert steps.index(by_name["Verify container version"]) < steps.index(
        export,
    )
    assert steps.index(export) < steps.index(upload)
    assert steps.index(upload) < steps.index(by_name["Stop container"])


def test_full_artifacts_cover_both_production_architectures() -> None:
    workflow = _load_workflow("docker-release.yml")
    inputs = workflow["on"]["workflow_dispatch"]["inputs"]
    assert inputs["full_artifacts"]["default"] == "false"
    job = workflow["jobs"]["build-artifacts"]
    matrix = job["strategy"]["matrix"]["include"]
    assert {item["arch"] for item in matrix} == {"amd64", "arm64"}
    steps = {step.get("name"): step for step in job["steps"]}
    build = steps["Build with production ACR defaults"]["run"]
    assert '--platform "linux/$ARCH"' in build
    assert "QWENPAW_DISABLED_CHANNELS=imessage" in build
    assert "NODE_IMAGE=" not in build
    assert "UV_IMAGE=" not in build
    assert "--push" not in build
    assert "type=oci" in build
    verify = steps["Load and verify the exported image"]["run"]
    assert "oci-archive:" in verify
    assert "verify-docker-artifacts.sh" in verify
    assert workflow["jobs"]["assemble-artifact"]["needs"] == [
        "build-artifacts",
    ]
    probe = (REPO_ROOT / ".github/scripts/docker-native-probe.py").read_text(
        encoding="utf-8"
    )
    assert 'if __name__ == "__main__":' in probe
    assert "def main():" in probe


def test_downloaded_artifact_regression_uses_native_architectures() -> None:
    workflow = _load_workflow("docker-release.yml")
    inputs = workflow["on"]["workflow_dispatch"]["inputs"]
    assert inputs["artifact_run_id"]["default"] == ""
    job = workflow["jobs"]["artifact-regression"]
    matrix = job["strategy"]["matrix"]["include"]
    assert {item["arch"]: item["runner"] for item in matrix} == {
        "amd64": "ubuntu-latest",
        "arm64": "ubuntu-24.04-arm",
    }
    steps = {step.get("name"): step for step in job["steps"]}
    assert (
        steps["Download final single-architecture artifact"]["with"]["run-id"]
        == "${{ inputs.artifact_run_id }}"
    )
    assert (
        steps["Download final multi-architecture artifact"]["with"]["run-id"]
        == "${{ inputs.oci_run_id || inputs.artifact_run_id }}"
    )
    regression = steps["Verify distribution and complete isolated regression"]
    assert "--oci" in regression["run"]
    assert "--legacy" in regression["run"]
    assert "--push" not in regression["run"]
    for name in (
        "verify-docker",
        "build-and-push",
        "build-artifacts",
        "assemble-artifact",
    ):
        assert "inputs.artifact_run_id == ''" in workflow["jobs"][name]["if"]
