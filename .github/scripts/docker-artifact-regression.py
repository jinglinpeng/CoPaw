"""Verify downloaded images using only fresh, labelled test containers."""

import argparse
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import secrets
import shutil
import sqlite3
import subprocess
import time
import urllib.error
import urllib.request
import zipfile


SCRIPTS = Path(__file__).resolve().parent
PREFIX = "/api/docker-artifact-regression"
LOCAL = urllib.request.build_opener(urllib.request.ProxyHandler({}))
OLD_MANIFESTS = {
    "amd64": (
        "1987eed11fbcd5d594f219ff34156e81f" "b7dee6120117f5ce0f647837ef5a31b"
    ),
    "arm64": (
        "9ea8531d57c7f6b117c2f9854750099b9" "616b1b82ce969c35b5a15da13595967"
    ),
}


def checksum(directory, filename):
    expected, listed = (directory / "SHA256SUMS").read_text().strip().split()
    assert listed == filename
    with (directory / filename).open("rb") as stream:
        actual = hashlib.file_digest(stream, "sha256").hexdigest()
    assert actual == expected
    return actual


def fingerprint(data):
    workspace = data / "working/workspaces/default"
    marker = workspace / "docker-artifact-marker.md"
    with sqlite3.connect(workspace / "docker-artifact-fixture.sqlite3") as db:
        value = db.execute("select value from smoke").fetchone()[0]
    assert value == marker.read_text(encoding="utf-8")
    return {
        "marker_sha256": hashlib.sha256(marker.read_bytes()).hexdigest(),
        "sqlite_value_sha256": hashlib.sha256(value.encode()).hexdigest(),
        "secret_sha256": hashlib.sha256(
            (data / "working.secret/artifact-secret-marker.txt").read_bytes()
        ).hexdigest(),
    }


def request(port, path, body=None, method=None, token=None, expected=200):
    headers = {"X-Agent-Id": "default"}
    if body is not None:
        headers["Content-Type"] = "application/json"
        body = json.dumps(body).encode()
    if token:
        headers["Authorization"] = "Bearer " + token
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}" + path,
        data=body,
        headers=headers,
        method=method,
    )
    try:
        response = LOCAL.open(req, timeout=300)
    except urllib.error.HTTPError as error:
        response = error
    with response:
        content = response.read()
        assert response.code == expected, (path, response.code, content[:500])
        if "application/json" in response.headers.get("Content-Type", ""):
            return json.loads(content)
        return content


def wait_ready(port):
    for _ in range(120):
        try:
            return request(port, "/api/version")
        except (OSError, AssertionError):
            time.sleep(2)
    raise RuntimeError(f"Container on {port} did not become ready")


class Regression:
    def __init__(self, args):
        self.args = args
        self.root = args.root.resolve()
        self.root.mkdir(parents=True, exist_ok=False)
        self.uid = secrets.token_hex(6)
        self.containers = {}
        self.results = {}
        self.kept = None

    def docker(self, *args):
        result = subprocess.run(
            [self.args.docker, *map(str, args)],
            text=True,
            encoding="utf-8",
            capture_output=True,
            timeout=1200,
        )
        if result.returncode:
            (self.root / "last-command-error.log").write_text(
                result.stdout + "\n" + result.stderr, encoding="utf-8"
            )
            raise RuntimeError(result.stderr[-3000:] or result.stdout[-3000:])
        return result.stdout.strip()

    def record(self, stage, value):
        self.results[stage] = value
        (self.root / "results.json").write_text(
            json.dumps(self.results, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        print("PASSED " + stage + ": " + json.dumps(value), flush=True)

    def start(self, kind, image, port, data=None, auth=False):
        data = data or self.root / kind
        mounts = []
        for folder in ("working", "working.secret", "working.backups"):
            path = data / folder
            path.mkdir(parents=True, exist_ok=True)
            mounts.extend(["-v", f"{path}:/app/{folder}"])
        name = f"qwenpaw-7298-{kind}-{self.args.arch}-{self.uid}"
        flags = ["-e", "QWENPAW_AUTH_ENABLED=true"] if auth else []
        internal_port = 8188 if auth else 8088
        image_id = json.loads(self.docker("image", "inspect", image))[0]["Id"]
        identity = self.docker(
            "run",
            "-d",
            "--name",
            name,
            "--label",
            "io.codex.artifact-regression=" + self.uid,
            "--platform",
            "linux/" + self.args.arch,
            "-p",
            f"127.0.0.1:{port}:{internal_port}",
            "-e",
            f"QWENPAW_PORT={internal_port}",
            *flags,
            *mounts,
            image_id,
        )
        self.containers[name] = (identity, image_id)
        wait_ready(port)
        return name, data

    def remove(self, name):
        identity, image_id = self.containers[name]
        inspection = json.loads(self.docker("inspect", name))[0]
        assert inspection["Id"] == identity
        assert inspection["Image"] == image_id
        assert (
            inspection["Config"]["Labels"]["io.codex.artifact-regression"]
            == self.uid
        )
        (self.root / (name + ".app-error.log")).write_text(
            self.docker(
                "exec", name, "tail", "-n", "120", "/var/log/app.err.log"
            ),
            encoding="utf-8",
        )
        self.docker("stop", "--time", "40", name)
        self.docker("rm", name)
        del self.containers[name]

    def copy_fixtures(self, name):
        self.docker(
            "cp",
            SCRIPTS / "docker-regression-fixtures",
            name + ":/app/working/regression-fixtures",
        )

    def plugin(self, port, install=False, force=False, modern=True):
        if install:
            for _ in range(120):
                try:
                    result = request(
                        port,
                        "/api/plugins/install",
                        {
                            "source": "/app/working/regression-fixtures/plugin",
                            "force": force,
                        },
                    )
                    break
                except AssertionError as error:
                    if error.args[0][1] != 503:
                        raise
                    time.sleep(1)
            else:
                raise RuntimeError("Plugin installer never became ready")
            assert result["loaded"]
        for _ in range(120):
            try:
                result = request(port, PREFIX + "/probe")
                break
            except AssertionError as error:
                if error.args[0][1] not in (404, 503):
                    raise
                time.sleep(1)
        else:
            raise RuntimeError("Plugin never finished loading")
        assert result["executable"] == "/app/venv/bin/python"
        if modern:
            assert result["python"].startswith("3.11.")
            assert result["openssl"].startswith("OpenSSL 3.5.")
        assert result["dependencies"] == {
            "pyfiglet": "1.0.4",
            "xxhash": "3.6.0",
        }
        assert all(
            path.startswith("/app/venv/")
            for path in result["dependency_paths"]
        )
        return result

    def mcp(self, port, create):
        for language in ("python", "node"):
            key = "artifact_" + language
            if create:
                extension = "py" if language == "python" else "js"
                request(
                    port,
                    "/api/mcp",
                    {
                        "client_key": key,
                        "client": {
                            "name": "Artifact " + language,
                            "command": language,
                            "args": [
                                "/app/working/regression-fixtures/"
                                f"mcp-{language}.{extension}"
                            ],
                            "transport": "stdio",
                            "enabled": True,
                        },
                    },
                    expected=201,
                )
            for _ in range(60):
                try:
                    tools = request(port, "/api/mcp/tools/" + key)
                    break
                except AssertionError as error:
                    if error.args[0][1] not in (502, 503):
                        raise
                    time.sleep(1)
            else:
                raise RuntimeError("MCP never became active: " + key)
            assert any(
                tool["name"].endswith("artifact_echo") for tool in tools
            ), tools

    def run(self):
        args = self.args
        artifact = args.artifact.resolve()
        digest = checksum(artifact, f"qwenpaw-{args.arch}.tar.gz")
        if not args.loaded:
            self.docker(
                "load", "--input", artifact / f"qwenpaw-{args.arch}.tar.gz"
            )
        image = "qwenpaw-verify:" + args.arch
        expected = json.loads((artifact / "build-metadata.json").read_text())[
            "containerimage.config.digest"
        ]
        inspection = json.loads(self.docker("image", "inspect", image))[0]
        assert inspection["Id"] == expected
        assert inspection["Architecture"] == args.arch
        self.record(
            "downloaded_docker_archive",
            {"sha256": digest, "image_id": expected},
        )
        if args.oci:
            directory = args.oci.resolve()
            archive = directory / "qwenpaw-image.oci.tar"
            digest = checksum(directory, archive.name)
            spec = importlib.util.spec_from_file_location(
                "oci_verify", SCRIPTS / "verify-distributed-oci.py"
            )
            verifier = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(verifier)
            result = verifier.verify(archive, directory / "index.json")
            assert result["configs"][args.arch] == expected
            subprocess.run(
                [
                    "skopeo",
                    "copy",
                    "--override-os",
                    "linux",
                    "--override-arch",
                    args.arch,
                    "oci-archive:" + str(archive),
                    "docker-daemon:qwenpaw-regression-oci:" + args.arch,
                ],
                check=True,
                timeout=600,
            )
            identity = json.loads(
                self.docker(
                    "image", "inspect", "qwenpaw-regression-oci:" + args.arch
                )
            )[0]["Id"]
            assert identity == expected
            self.record(
                "downloaded_multiarch_oci", {**result, "sha256": digest}
            )
        name, data = self.start("fresh", image, args.port)
        (data / "working.secret/artifact-secret-marker.txt").write_bytes(
            b"source-test-secret-marker-not-a-credential"
        )
        missing = json.loads(
            self.docker(
                "exec",
                name,
                "/app/venv/bin/python",
                "-c",
                "import importlib.util,json;print(json.dumps({"
                "n:importlib.util.find_spec(n) is None "
                "for n in ('pyfiglet','xxhash')}))",
            )
        )
        assert all(missing.values()), missing
        self.copy_fixtures(name)
        self.record("plugin_hot_install", self.plugin(args.port, install=True))
        assert request(args.port, PREFIX + "/tools", {})["passed"]
        self.record(
            "plugin_reinstall",
            self.plugin(args.port, install=True, force=True),
        )
        for script in (
            "docker-native-probe.py",
            "docker-regression-desktop.py",
        ):
            self.docker("cp", SCRIPTS / script, name + ":/tmp/" + script)
            result = json.loads(
                self.docker(
                    "exec",
                    "-e",
                    "DISPLAY=:1",
                    name,
                    "/app/venv/bin/python",
                    "/tmp/" + script,
                )
            )
            self.record(script.removesuffix(".py"), result)
            for screenshot in result.get("screenshots", []):
                self.docker(
                    "cp",
                    name + ":" + screenshot,
                    self.root / Path(screenshot).name,
                )
        self.mcp(args.port, create=True)
        chat = request(
            args.port,
            "/api/chats",
            {
                "name": "中文产物持久化测试",
                "session_id": "artifact-test",
                "user_id": "artifact-test",
                "channel": "console",
            },
        )
        before = self.plugin(args.port)
        saved = fingerprint(data)
        assert (
            self.docker(
                "exec", name, "/app/venv/bin/python", "-m", "pip", "check"
            )
            == "No broken requirements found."
        )
        self.remove(name)
        name, _ = self.start("recreated", image, args.port, data=data)
        after = self.plugin(args.port)
        assert after["marker_sha256"] == before["marker_sha256"]
        assert fingerprint(data) == saved
        assert any(
            item["id"] == chat["id"]
            for item in request(args.port, "/api/chats")
        )
        self.mcp(args.port, create=False)
        for language in ("python", "node"):
            request(
                args.port, "/api/mcp/artifact_" + language, method="DELETE"
            )
        self.record(
            "container_recreation", {"plugin": after, "chat_id": chat["id"]}
        )
        self.backup(name, image, chat)
        self.authentication(image)
        if args.legacy:
            self.upgrade(image)
        if args.keep:
            self.kept = name
        self.record(
            "complete",
            {
                "url": f"http://127.0.0.1:{args.port}",
                "container": name,
                "kept": bool(args.keep),
            },
        )

    def backup(self, name, image, chat):
        port = self.args.port
        job = request(
            port,
            "/api/backups/jobs",
            {
                "name": "Artifact regression",
                "agents": ["default"],
                "scope": {
                    "include_agents": True,
                    "include_global_config": True,
                    "include_secrets": False,
                    "include_skill_pool": False,
                },
            },
            expected=202,
        )
        for _ in range(120):
            result = request(port, "/api/backups/jobs/" + job["job_id"])
            if result["status"] in ("completed", "failed", "cancelled"):
                break
            time.sleep(1)
        assert result["status"] == "completed", result
        backup_id = result["result"]["id"]
        exported = request(port, f"/api/backups/{backup_id}/export")
        archive = self.root / "backup-export.zip"
        archive.write_bytes(exported)
        with zipfile.ZipFile(io.BytesIO(exported)) as zip_file:
            assert zip_file.testzip() is None
            assert "data/config.json" in zip_file.namelist()
            assert not any(
                item.startswith("data/secrets/")
                for item in zip_file.namelist()
            )
        restored, data = self.start("restore", image, port + 1)
        secret_marker = data / "working.secret/artifact-secret-marker.txt"
        secret_marker.write_bytes(b"destination-test-secret-preserved")
        boundary = "artifact" + self.uid
        body = (
            (
                f"--{boundary}\r\n"
                'Content-Disposition: form-data; name="trust_mode"\r\n\r\n'
                "foreign\r\n"
                f"--{boundary}\r\n"
                'Content-Disposition: form-data; name="file"; '
                'filename="backup.zip"\r\n'
                "Content-Type: application/zip\r\n\r\n"
            ).encode()
            + exported
            + f"\r\n--{boundary}--\r\n".encode()
        )
        upload = urllib.request.Request(
            f"http://127.0.0.1:{port + 1}/api/backups/import",
            data=body,
            headers={
                "Content-Type": "multipart/form-data; boundary=" + boundary
            },
        )
        with LOCAL.open(upload, timeout=120) as response:
            imported = json.load(response)
        request(
            port + 1,
            f"/api/backups/{imported['id']}/restore",
            {
                "include_agents": True,
                "agent_ids": ["default"],
                "include_global_config": True,
                "include_secrets": False,
                "include_skill_pool": False,
                "mode": "full",
                "trust_mode": "foreign",
            },
        )
        assert any(
            item["id"] == chat["id"]
            for item in request(port + 1, "/api/chats")
        )
        source = self.plugin(port)["marker_sha256"]
        marker = data / "working/workspaces/default/docker-artifact-marker.md"
        assert hashlib.sha256(marker.read_bytes()).hexdigest() == source
        restored_state = fingerprint(data)
        assert restored_state["marker_sha256"] == source
        assert (
            secret_marker.read_bytes() == b"destination-test-secret-preserved"
        )
        self.record(
            "backup_export_import_restore",
            {
                "backup_id": backup_id,
                "zip_bytes": len(exported),
                "marker_sha256": source,
                "chat_id": chat["id"],
            },
        )
        self.remove(restored)

    def continue_run(self, data):
        data = data.resolve()
        saved = fingerprint(data)
        metadata = json.loads(
            (self.args.artifact / "build-metadata.json").read_text()
        )
        image = "qwenpaw-verify:" + self.args.arch
        actual = json.loads(self.docker("image", "inspect", image))[0]
        assert actual["Id"] == metadata["containerimage.config.digest"]
        name, _ = self.start("continued", image, self.args.port, data=data)
        restored_plugin = self.plugin(self.args.port)
        assert fingerprint(data) == saved
        chats = request(self.args.port, "/api/chats")
        chat = next(
            item for item in chats if item["session_id"] == "artifact-test"
        )
        assert chat["name"] == "中文产物持久化测试"
        self.mcp(self.args.port, create=False)
        for language in ("python", "node"):
            request(
                self.args.port,
                "/api/mcp/artifact_" + language,
                method="DELETE",
            )
        self.record(
            "container_recreation",
            {
                "plugin": restored_plugin,
                "fingerprint": saved,
                "chat_id": chat["id"],
                "continued_test_data": str(data),
            },
        )
        self.backup(name, image, chat)
        self.authentication(image)
        if self.args.legacy:
            self.upgrade(image)
        if self.args.keep:
            self.kept = name
        self.record(
            "complete",
            {
                "url": f"http://127.0.0.1:{self.args.port}",
                "container": name,
                "kept": bool(self.args.keep),
            },
        )

    def authentication(self, image):
        port = self.args.port + 2
        name, _ = self.start("auth", image, port, auth=True)
        password = secrets.token_urlsafe(24)
        registered = request(
            port,
            "/api/auth/register",
            {
                "username": "artifact-user",
                "password": password,
            },
        )
        token = registered["token"]
        request(port, "/api/chats", expected=401)
        request(
            port, "/api/chats", token="invalid-artifact-token", expected=401
        )
        request(port, "/api/chats", token=token)
        logged_in = request(
            port,
            "/api/auth/login",
            {
                "username": "artifact-user",
                "password": password,
            },
        )
        assert logged_in["token"]
        self.docker("restart", "--time", "40", name)
        wait_ready(port)
        assert request(port, "/api/auth/verify", token=token)["valid"]
        self.record("authentication_custom_port_restart", "passed")
        self.remove(name)

    def upgrade(self, image):
        port = self.args.port + 3
        legacy = (
            "docker.io/agentscope/qwenpaw@sha256:"
            + OLD_MANIFESTS[self.args.arch]
        )
        self.docker("pull", "--platform", "linux/" + self.args.arch, legacy)
        name, data = self.start("legacy", legacy, port)
        (data / "working.secret/artifact-secret-marker.txt").write_bytes(
            b"legacy-test-secret-preserved"
        )
        old_version = request(port, "/api/version")
        assert old_version["version"] == "2.2.1"
        self.copy_fixtures(name)
        old_runtime = self.plugin(port, install=True, modern=False)
        assert request(port, PREFIX + "/tools", {})["passed"]
        old_runtime = self.plugin(port, modern=False)
        saved = fingerprint(data)
        chat = request(
            port,
            "/api/chats",
            {
                "name": "旧版升级中文测试",
                "session_id": "artifact-upgrade",
                "user_id": "artifact-test",
                "channel": "console",
            },
        )
        self.remove(name)
        name, _ = self.start("upgraded", image, port, data=data)
        new_runtime = self.plugin(port)
        assert old_runtime["marker_sha256"] == new_runtime["marker_sha256"]
        assert fingerprint(data) == saved
        assert any(
            item["id"] == chat["id"] for item in request(port, "/api/chats")
        )
        assert request(port, PREFIX + "/tools", {})["passed"]
        self.record(
            "upgrade_v2_2_1",
            {
                "old": old_runtime,
                "new": new_runtime,
                "chat_id": chat["id"],
                "legacy_manifest": legacy,
            },
        )
        self.remove(name)

    def cleanup(self):
        for name in list(self.containers):
            if name == self.kept:
                continue
            try:
                (self.root / (name + ".log")).write_text(
                    self.docker("logs", "--tail", "150", name),
                    encoding="utf-8",
                )
                self.remove(name)
            except Exception as error:
                print("Cleanup needs attention: " + str(error), flush=True)
                if name in self.containers:
                    self.remove(name)
        for kind in ("fresh", "restore", "legacy", "auth"):
            directory = self.root / kind / "working"
            for screenshot in directory.glob("artifact-*.png"):
                shutil.copyfile(screenshot, self.root / screenshot.name)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--artifact", type=Path, required=True)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--arch", choices=("amd64", "arm64"), required=True)
    parser.add_argument("--oci", type=Path)
    parser.add_argument("--docker", default="docker")
    parser.add_argument("--port", type=int, default=18188)
    parser.add_argument("--legacy", action="store_true")
    parser.add_argument("--keep", action="store_true")
    parser.add_argument("--loaded", action="store_true")
    parser.add_argument("--resume-data", type=Path)
    regression = Regression(parser.parse_args())
    try:
        if regression.args.resume_data:
            regression.continue_run(regression.args.resume_data)
        else:
            regression.run()
    finally:
        regression.cleanup()


if __name__ == "__main__":
    main()
