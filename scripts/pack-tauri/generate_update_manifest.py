#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Tauri updater helper: stage per-platform artifacts and build the manifest.

Subcommands:
  stage     Copy a Tauri-built updater archive (and its .sig) into the dist
            tree, then write a small JSON sidecar describing it.
  manifest  Aggregate one or more stage-produced sidecar JSON files into the
            unified `qwenpaw-tauri-latest.json` consumed by tauri-plugin-updater.
"""

from __future__ import annotations

import argparse
import json
import platform as _platform
import re
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote


_PEP440_RE = re.compile(
    r"^(\d+)\.(\d+)\.(\d+)(?:(a|b|rc)(\d+))?(?:\.post(\d+))?(?:\.dev(\d+))?$",
)


def to_semver(version: str) -> str:
    match = _PEP440_RE.match(version)
    if not match:
        raise SystemExit(f"unsupported Python version for Tauri: {version}")
    major, minor, patch, prerelease, prerelease_n, post, dev = match.groups()
    prerelease_map = {"a": "alpha", "b": "beta", "rc": "rc"}
    labels: list[str] = []
    if prerelease:
        labels.append(f"{prerelease_map[prerelease]}.{prerelease_n}")
    if post:
        labels.append(f"post.{post}")
    if dev:
        labels.append(f"dev.{dev}")
    suffix = f"-{'.'.join(labels)}" if labels else ""
    return f"{major}.{minor}.{patch}{suffix}"


def auto_target() -> str:
    arch_map = {
        "amd64": "x86_64",
        "x86_64": "x86_64",
        "arm64": "aarch64",
        "aarch64": "aarch64",
    }
    arch = arch_map.get(
        _platform.machine().lower(),
        _platform.machine().lower(),
    )
    if sys.platform == "darwin":
        return f"darwin-{arch}"
    if sys.platform == "win32":
        return f"windows-{arch}"
    return f"linux-{arch}"


# ─────────────────────────── stage ───────────────────────────


def _find_source(bundle_dir: Path, pattern: str) -> Path:
    matches = sorted(bundle_dir.glob(pattern))
    if not matches:
        raise SystemExit(
            f"no artifact matching {pattern!r} under {bundle_dir}",
        )
    return matches[0]


def cmd_stage(args: argparse.Namespace) -> None:
    bundle_dir = Path(args.bundle_dir)
    source = _find_source(bundle_dir, args.pattern)
    sig_source = source.with_suffix(source.suffix + ".sig")
    if not sig_source.is_file():
        raise SystemExit(f"no updater signature found at {sig_source}")

    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, output)
    shutil.copyfile(sig_source, output.with_suffix(output.suffix + ".sig"))

    target = args.target if args.target != "auto" else auto_target()
    metadata = {
        "target": target,
        "artifact": output.name,
        "signature": output.name + ".sig",
    }
    sidecar = output.parent / f"tauri-{target}-updater.json"
    sidecar.write_text(
        json.dumps(metadata, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    print(f"staged {output.name} ({target}); sidecar {sidecar.name}")


# ─────────────────────────── manifest ───────────────────────────


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


def _signature_text(path: Path) -> str:
    if not path.is_file():
        raise SystemExit(f"signature file not found: {path}")
    return path.read_text(encoding="utf-8-sig").strip()


def cmd_manifest(args: argparse.Namespace) -> None:
    platforms: dict[str, dict[str, str]] = {}
    for raw in args.metadata:
        meta_path = Path(raw)
        meta = _read_metadata(meta_path)
        workdir = meta_path.parent
        artifact_path = workdir / meta["artifact"]
        if not artifact_path.is_file():
            raise SystemExit(f"artifact file not found: {artifact_path}")
        platforms[meta["target"]] = {
            "url": f"{args.base_url.rstrip('/')}/{quote(meta['artifact'])}",
            "signature": _signature_text(workdir / meta["signature"]),
        }
    if not platforms:
        raise SystemExit("no updater platforms were provided")

    manifest = {
        "version": to_semver(args.version),
        "notes": args.notes,
        "pub_date": args.pub_date,
        "platforms": platforms,
    }
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2, ensure_ascii=False)
        f.write("\n")
    print(
        f"wrote manifest {output} (platforms: {', '.join(sorted(platforms))})",
    )


# ─────────────────────────── cli ───────────────────────────


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_stage = sub.add_parser(
        "stage",
        help="Copy a Tauri updater archive + .sig into dist and write a sidecar.",
    )
    p_stage.add_argument(
        "--bundle-dir",
        required=True,
        help="Tauri bundle output dir (e.g., target/release/bundle/nsis).",
    )
    p_stage.add_argument(
        "--pattern",
        required=True,
        help="Glob to find the artifact (e.g., '*-setup.exe', '*.app.tar.gz').",
    )
    p_stage.add_argument(
        "--target",
        default="auto",
        help="Updater target (e.g., windows-x86_64, darwin-aarch64) or 'auto'.",
    )
    p_stage.add_argument(
        "--output",
        required=True,
        help="Destination artifact path; .sig is staged alongside.",
    )
    p_stage.set_defaults(func=cmd_stage)

    p_manifest = sub.add_parser(
        "manifest",
        help="Aggregate per-platform sidecars into the updater manifest JSON.",
    )
    p_manifest.add_argument("--version", required=True)
    p_manifest.add_argument("--base-url", required=True)
    p_manifest.add_argument(
        "--metadata",
        action="append",
        default=[],
        help="Path to a sidecar JSON file (repeatable).",
    )
    p_manifest.add_argument("--notes", default="")
    p_manifest.add_argument(
        "--pub-date",
        default=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    )
    p_manifest.add_argument("--output", required=True)
    p_manifest.set_defaults(func=cmd_manifest)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
