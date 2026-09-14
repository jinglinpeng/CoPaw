"""Merge verified OCI exports, including nested indexes and attestations."""

import hashlib
import json
from pathlib import Path
import sys


root = Path(sys.argv[1])
descriptors = {}
architectures = set()


def read_blob(directory, descriptor):
    algorithm, digest = descriptor["digest"].split(":", 1)
    assert algorithm == "sha256"
    content = (directory / "blobs" / algorithm / digest).read_bytes()
    assert hashlib.sha256(content).hexdigest() == digest
    return json.loads(content)


def visit(directory, descriptor, expected_architecture):
    blob = read_blob(directory, descriptor)
    if "manifests" in blob:
        for child in blob["manifests"]:
            visit(directory, child, expected_architecture)
        return
    assert "config" in blob and "layers" in blob
    config = read_blob(directory, blob["config"])
    if config.get("os") == "linux":
        architecture = config["architecture"]
        assert architecture == expected_architecture
        architectures.add(architecture)
        descriptor["platform"] = {"os": "linux", "architecture": architecture}
        if config.get("variant"):
            descriptor["platform"]["variant"] = config["variant"]
    descriptors[descriptor["digest"]] = descriptor


for arch in ("amd64", "arm64"):
    source = root / arch
    index = json.loads((source / "index.json").read_text())
    for item in index["manifests"]:
        visit(source, item, arch)
assert architectures == {"amd64", "arm64"}
merged = {
    "schemaVersion": 2,
    "mediaType": "application/vnd.oci.image.index.v1+json",
    "manifests": list(descriptors.values()),
}
(root / "merged" / "index.json").write_text(json.dumps(merged, indent=2))
print("Verified multi-arch OCI index:", sorted(architectures))
