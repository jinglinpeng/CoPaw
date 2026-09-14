"""Validate every OCI blob and select both architecture manifests."""

import hashlib
import json
from pathlib import Path
import sys
import tarfile


def verify(archive, external_index):
    blobs = {}
    documents = {}
    index = None
    with tarfile.open(archive, "r|") as tar:
        for member in tar:
            if not member.isfile():
                continue
            name = member.name.removeprefix("./")
            if name in ("index.json", "oci-layout"):
                document = json.load(tar.extractfile(member))
                if name == "index.json":
                    index = document
                else:
                    assert document["imageLayoutVersion"] == "1.0.0"
                continue
            assert name.startswith("blobs/sha256/"), name
            expected = name.split("/")[-1]
            digest = hashlib.sha256()
            content = bytearray()
            with tar.extractfile(member) as source:
                while chunk := source.read(1024 * 1024):
                    digest.update(chunk)
                    if member.size < 1024 * 1024:
                        content.extend(chunk)
            assert digest.hexdigest() == expected, name
            blobs["sha256:" + expected] = member.size
            if content:
                try:
                    documents["sha256:" + expected] = json.loads(content)
                except (ValueError, UnicodeDecodeError):
                    pass
    assert index == json.loads(Path(external_index).read_text())
    configs = {}

    def visit(descriptor):
        assert blobs[descriptor["digest"]] == descriptor["size"]
        manifest = documents[descriptor["digest"]]
        if "manifests" in manifest:
            for child in manifest["manifests"]:
                visit(child)
            return
        for reference in [manifest["config"], *manifest["layers"]]:
            assert blobs[reference["digest"]] == reference["size"]
        config = documents[manifest["config"]["digest"]]
        if config.get("os") == "linux":
            arch = config["architecture"]
            assert descriptor["platform"]["architecture"] == arch
            assert arch not in configs
            configs[arch] = manifest["config"]["digest"]

    for descriptor in index["manifests"]:
        visit(descriptor)
    assert set(configs) == {"amd64", "arm64"}
    return {"configs": configs, "verified_blobs": len(blobs)}


if __name__ == "__main__":
    print(json.dumps(verify(sys.argv[1], sys.argv[2])))
