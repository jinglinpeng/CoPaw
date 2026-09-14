"""Small fixtures for downloaded archive integrity and cleanup safety."""

import hashlib
import importlib.util
import io
import json
from pathlib import Path
import tarfile
from types import SimpleNamespace

import pytest


SCRIPTS = Path(__file__).resolve().parents[3] / ".github/scripts"


def load_script(filename):
    spec = importlib.util.spec_from_file_location(filename, SCRIPTS / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def archive_fixture(tmp_path, corrupt=False):
    members = {"oci-layout": b'{"imageLayoutVersion":"1.0.0"}'}

    def blob(value):
        data = json.dumps(value).encode() if isinstance(value, dict) else value
        digest = hashlib.sha256(data).hexdigest()
        members["blobs/sha256/" + digest] = data
        return {"digest": "sha256:" + digest, "size": len(data)}

    manifests = []
    configs = {}
    for arch in ("amd64", "arm64"):
        config = blob({"os": "linux", "architecture": arch})
        configs[arch] = config["digest"]
        manifest = blob({"config": config, "layers": [blob(arch.encode())]})
        manifest["platform"] = {"os": "linux", "architecture": arch}
        manifests.append(manifest)
    index = {"schemaVersion": 2, "manifests": manifests}
    members["index.json"] = json.dumps(index).encode()
    index_file = tmp_path / "index.json"
    index_file.write_bytes(members["index.json"])
    if corrupt:
        members[
            next(name for name in members if name.startswith("blobs/"))
        ] = b"corrupt"
    archive = tmp_path / "image.tar"
    with tarfile.open(archive, "w") as output:
        for name, data in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            output.addfile(info, io.BytesIO(data))
    return archive, index_file, configs


def test_oci_verifies_all_blobs_and_both_configs(tmp_path):
    module = load_script("verify-distributed-oci.py")
    archive, index, configs = archive_fixture(tmp_path)
    assert module.verify(archive, index) == {
        "configs": configs,
        "verified_blobs": 6,
    }


def test_oci_rejects_corrupt_blob(tmp_path):
    module = load_script("verify-distributed-oci.py")
    archive, index, _ = archive_fixture(tmp_path, corrupt=True)
    with pytest.raises(AssertionError):
        module.verify(archive, index)


def test_named_nested_oci_preserves_both_image_configs(tmp_path):
    archive, _, configs = archive_fixture(tmp_path)
    source = tmp_path / "layout"
    source.mkdir()
    with tarfile.open(archive) as tar:
        for member in tar:
            target = source / member.name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(tar.extractfile(member).read())
    wrapper = load_script("wrap-docker-oci.py")
    index = wrapper.wrap(source)
    assert len(index["manifests"]) == 1
    descriptor = index["manifests"][0]
    assert (
        descriptor["annotations"]["org.opencontainers.image.ref.name"]
        == "qwenpaw-image"
    )
    nested_archive = tmp_path / "nested.tar"
    with tarfile.open(nested_archive, "w") as tar:
        for path in source.rglob("*"):
            if path.is_file():
                tar.add(path, arcname=path.relative_to(source))
    verifier = load_script("verify-distributed-oci.py")
    assert verifier.verify(nested_archive, source / "index.json") == {
        "configs": configs,
        "verified_blobs": 7,
    }


def test_archive_checksum_rejects_changed_payload(tmp_path):
    module = load_script("docker-artifact-regression.py")
    data = b"test archive bytes"
    digest = hashlib.sha256(data).hexdigest()
    (tmp_path / "image.tar.gz").write_bytes(data)
    (tmp_path / "SHA256SUMS").write_text(digest + "  image.tar.gz\n")
    assert module.checksum(tmp_path, "image.tar.gz") == digest
    (tmp_path / "image.tar.gz").write_bytes(b"modified")
    with pytest.raises(AssertionError):
        module.checksum(tmp_path, "image.tar.gz")


def test_cleanup_refuses_container_with_changed_identity():
    module = load_script("docker-artifact-regression.py")
    regression = module.Regression.__new__(module.Regression)
    regression.uid = "unique-test"
    regression.containers = {"test-container": ("expected-id", "image-id")}
    calls = []

    def fake_docker(*arguments):
        calls.append(arguments)
        return json.dumps([{"Id": "different-id"}])

    regression.docker = fake_docker
    with pytest.raises(AssertionError):
        regression.remove("test-container")
    assert calls == [("inspect", "test-container")]


def test_regression_refuses_existing_data_directory(tmp_path):
    module = load_script("docker-artifact-regression.py")
    with pytest.raises(FileExistsError):
        module.Regression(SimpleNamespace(root=tmp_path))
