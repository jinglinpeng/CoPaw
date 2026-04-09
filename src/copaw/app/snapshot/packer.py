# -*- coding: utf-8 -*-
"""SnapshotPacker: ZIP packing/unpacking with manifest and checksums."""
from __future__ import annotations

import hashlib
import io
import json
import logging
import os
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Optional

from .models import SnapshotManifest

logger = logging.getLogger(__name__)

MANIFEST_NAME = "manifest.json"

# Resource limits for import validation
MAX_TOTAL_SIZE = 2 * 1024 * 1024 * 1024  # 2 GB
MAX_SINGLE_FILE = 500 * 1024 * 1024  # 500 MB
MAX_FILE_COUNT = 10_000
MAX_PATH_DEPTH = 20
MAX_PATH_LENGTH = 260
MAX_COMPRESSION_RATIO = 100


def _sha256_file(path: Path) -> str:
    """Compute SHA-256 hex digest of a file."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return f"sha256:{h.hexdigest()}"


def _is_safe_zip_path(name: str) -> bool:
    """Reject path traversal, symlinks, absolute paths."""
    if name.startswith("/") or name.startswith("\\"):
        return False
    parts = Path(name).parts
    if ".." in parts:
        return False
    if len(parts) > MAX_PATH_DEPTH:
        return False
    if len(name) > MAX_PATH_LENGTH:
        return False
    return True


class SnapshotPacker:
    """Packs collected files into a ZIP archive and unpacks them."""

    @staticmethod
    def pack(
        staging_dir: Path,
        manifest: SnapshotManifest,
        output_path: Path,
    ) -> Path:
        """Pack staging directory into a ZIP with manifest and checksums.

        Args:
            staging_dir: Directory containing files to pack.
            manifest: Snapshot manifest metadata.
            output_path: Where to write the ZIP file.

        Returns:
            Path to the created ZIP file.
        """
        checksums: Dict[str, str] = {}

        output_path.parent.mkdir(parents=True, exist_ok=True)

        with zipfile.ZipFile(
            output_path, "w", zipfile.ZIP_DEFLATED
        ) as zf:
            for root, dirs, files in os.walk(staging_dir):
                # Skip __pycache__ and hidden dirs
                dirs[:] = [
                    d for d in dirs
                    if d != "__pycache__" and not d.startswith(".")
                ]
                for fname in files:
                    fpath = Path(root) / fname
                    arcname = fpath.relative_to(staging_dir).as_posix()
                    checksums[arcname] = _sha256_file(fpath)
                    zf.write(fpath, arcname)

            manifest.file_checksums = checksums
            manifest_json = manifest.model_dump_json(indent=2)
            zf.writestr(MANIFEST_NAME, manifest_json)

        logger.info(
            "Packed snapshot: %s (%d files, %.1f MB)",
            output_path.name,
            len(checksums),
            output_path.stat().st_size / 1024 / 1024,
        )
        return output_path

    @staticmethod
    def unpack(
        zip_path: Path,
        dest_dir: Path,
        *,
        validate: bool = True,
    ) -> SnapshotManifest:
        """Unpack a snapshot ZIP, validate checksums, return manifest.

        Args:
            zip_path: Path to the ZIP file.
            dest_dir: Directory to extract into.
            validate: Whether to run resource limit and checksum validation.

        Returns:
            Parsed SnapshotManifest.

        Raises:
            ValueError: On validation failures.
        """
        if not zipfile.is_zipfile(zip_path):
            raise ValueError(f"Not a valid zip file: {zip_path}")

        with zipfile.ZipFile(zip_path, "r") as zf:
            if validate:
                SnapshotPacker._validate_zip_contents(zf)

            # Read manifest first
            if MANIFEST_NAME not in zf.namelist():
                raise ValueError("Snapshot missing manifest.json")

            manifest_data = json.loads(zf.read(MANIFEST_NAME))
            manifest = SnapshotManifest.model_validate(manifest_data)

            # Extract all files except manifest
            dest_dir.mkdir(parents=True, exist_ok=True)
            for info in zf.infolist():
                if info.filename == MANIFEST_NAME:
                    continue
                if not _is_safe_zip_path(info.filename):
                    logger.warning("Skipping unsafe path: %s", info.filename)
                    continue
                # Skip symlinks (external_attr bit 29)
                if info.external_attr >> 28 == 0xA:
                    logger.warning("Skipping symlink: %s", info.filename)
                    continue
                zf.extract(info, dest_dir)

        if validate:
            SnapshotPacker._verify_checksums(manifest, dest_dir)

        logger.info(
            "Unpacked snapshot: %d files into %s",
            len(manifest.file_checksums),
            dest_dir,
        )
        return manifest

    @staticmethod
    def read_manifest(zip_path: Path) -> SnapshotManifest:
        """Read only the manifest from a ZIP without extracting."""
        with zipfile.ZipFile(zip_path, "r") as zf:
            if MANIFEST_NAME not in zf.namelist():
                raise ValueError("Snapshot missing manifest.json")
            data = json.loads(zf.read(MANIFEST_NAME))
            return SnapshotManifest.model_validate(data)

    @staticmethod
    def _validate_zip_contents(zf: zipfile.ZipFile) -> None:
        """Check resource limits before extracting."""
        total_size = 0
        file_count = 0
        compressed_size = 0

        for info in zf.infolist():
            if info.filename == MANIFEST_NAME:
                continue

            if not _is_safe_zip_path(info.filename):
                continue

            file_count += 1
            if file_count > MAX_FILE_COUNT:
                raise ValueError(
                    f"Too many files: exceeds limit of {MAX_FILE_COUNT}"
                )

            if info.file_size > MAX_SINGLE_FILE:
                raise ValueError(
                    f"File too large: {info.filename} "
                    f"({info.file_size / 1024 / 1024:.1f} MB, "
                    f"limit {MAX_SINGLE_FILE / 1024 / 1024:.0f} MB)"
                )

            total_size += info.file_size
            compressed_size += info.compress_size

            if total_size > MAX_TOTAL_SIZE:
                raise ValueError(
                    f"Total size exceeds limit of "
                    f"{MAX_TOTAL_SIZE / 1024 / 1024 / 1024:.0f} GB"
                )

        if compressed_size > 0:
            ratio = total_size / compressed_size
            if ratio > MAX_COMPRESSION_RATIO:
                raise ValueError(
                    f"Suspicious compression ratio ({ratio:.0f}x), "
                    f"possible zip bomb"
                )

    @staticmethod
    def _verify_checksums(
        manifest: SnapshotManifest, dest_dir: Path
    ) -> None:
        """Verify SHA-256 checksums of extracted files."""
        for arcname, expected in manifest.file_checksums.items():
            fpath = dest_dir / arcname
            if not fpath.is_file():
                logger.warning("Missing file from manifest: %s", arcname)
                continue
            actual = _sha256_file(fpath)
            if actual != expected:
                raise ValueError(
                    f"Checksum mismatch for {arcname}: "
                    f"expected {expected}, got {actual}"
                )
