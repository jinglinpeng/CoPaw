#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Generate a Tauri updater manifest from staged release artifacts."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import re
from urllib.parse import quote


def _read_metadata(path: Path) -> dict[str, str]:
    with path.open("r", encoding="utf-8-sig") as f:
        data = json.load(f)
    required = {"target", "artifact", "signature"}
    missing = required - set(data)
    if missing:
        raise SystemExit(
            f"{path} missing required keys: {', '.join(sorted(missing))}",
        )
    return {key: str(data[key]) for key in required}


def _artifact_url(base_url: str, filename: str) -> str:
    return f"{base_url.rstrip('/')}/{quote(filename)}"


def _signature_text(workdir: Path, filename: str) -> str:
    signature_path = workdir / filename
    if not signature_path.is_file():
        raise SystemExit(f"signature file not found: {signature_path}")
    return signature_path.read_text(encoding="utf-8-sig").strip()


def to_semver(version: str) -> str:
    match = re.match(
        r"^(\d+)\.(\d+)\.(\d+)(?:(a|b|rc)(\d+))?(?:\.post(\d+))?(?:\.dev(\d+))?$",
        version,
    )
    if not match:
        raise SystemExit(f"unsupported Python version for Tauri: {version}")
    (
        major,
        minor,
        patch,
        prerelease,
        prerelease_number,
        post,
        dev,
    ) = match.groups()
    prerelease_map = {"a": "alpha", "b": "beta", "rc": "rc"}
    labels = []
    if prerelease:
        labels.append(f"{prerelease_map[prerelease]}.{prerelease_number}")
    if post:
        labels.append(f"post.{post}")
    if dev:
        labels.append(f"dev.{dev}")
    suffix = f"-{'.'.join(labels)}" if labels else ""
    return f"{major}.{minor}.{patch}{suffix}"


def build_manifest(
    version: str,
    base_url: str,
    metadata_files: list[Path],
    notes: str,
    pub_date: str,
) -> dict[str, object]:
    platforms: dict[str, dict[str, str]] = {}
    for metadata_file in metadata_files:
        metadata = _read_metadata(metadata_file)
        workdir = metadata_file.parent
        artifact = metadata["artifact"]
        artifact_path = workdir / artifact
        if not artifact_path.is_file():
            raise SystemExit(f"artifact file not found: {artifact_path}")
        platforms[metadata["target"]] = {
            "url": _artifact_url(base_url, artifact),
            "signature": _signature_text(workdir, metadata["signature"]),
        }

    if not platforms:
        raise SystemExit("no updater platforms were provided")

    return {
        "version": to_semver(version),
        "notes": notes,
        "pub_date": pub_date,
        "platforms": platforms,
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Generate the static JSON consumed by tauri-plugin-updater",
    )
    parser.add_argument("--version", required=True)
    parser.add_argument("--base-url", required=True)
    parser.add_argument(
        "--metadata",
        action="append",
        default=[],
        help="Path to a JSON file with target, artifact, and signature keys",
    )
    parser.add_argument("--notes", default="")
    parser.add_argument(
        "--pub-date",
        default=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    )
    parser.add_argument("--output", required=True)
    args = parser.parse_args()

    manifest = build_manifest(
        version=args.version,
        base_url=args.base_url,
        metadata_files=[Path(path) for path in args.metadata],
        notes=args.notes,
        pub_date=args.pub_date,
    )
    with Path(args.output).open("w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2, ensure_ascii=False)
        f.write("\n")


if __name__ == "__main__":
    main()
