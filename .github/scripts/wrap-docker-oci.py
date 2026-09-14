"""Expose one named multi-platform image in an OCI image layout."""

import hashlib
import json
from pathlib import Path
import sys


def wrap(directory):
    path = Path(directory) / "index.json"
    content = path.read_bytes()
    multiarch = json.loads(content)
    architectures = {
        item.get("platform", {}).get("architecture")
        for item in multiarch["manifests"]
        if item.get("platform", {}).get("os") == "linux"
    }
    assert architectures == {"amd64", "arm64"}
    digest = hashlib.sha256(content).hexdigest()
    blob = Path(directory) / "blobs/sha256" / digest
    blob.write_bytes(content)
    index = {
        "schemaVersion": 2,
        "mediaType": "application/vnd.oci.image.index.v1+json",
        "manifests": [
            {
                "mediaType": "application/vnd.oci.image.index.v1+json",
                "digest": "sha256:" + digest,
                "size": len(content),
                "annotations": {
                    "org.opencontainers.image.ref.name": "qwenpaw-image"
                },
            }
        ],
    }
    path.write_text(json.dumps(index, indent=2))
    return index


if __name__ == "__main__":
    print(json.dumps(wrap(sys.argv[1])))
