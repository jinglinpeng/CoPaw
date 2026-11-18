"""Exercise real native Linux desktop updates on an isolated Ubuntu runner."""

import base64
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import signal
import ssl
import subprocess
import sys
import threading
import time

from desktop_verify import SEL_INPUT, _http

PACKAGE = "qwen-paw-desktop"
EXECUTABLE = "/usr/bin/qwenpaw-desktop"
BASE = "2.3.1~beta.1"
NEW = "2.3.1"
ROOT = Path(os.environ["RUNNER_TEMP"]) / "linux-upgrade-evidence"
ROOT.mkdir(exist_ok=True)
UPDATES = Path(sys.argv[1]).resolve()


def installed_version():
    return subprocess.check_output(
        ["dpkg-query", "-W", "-f=${Version}", PACKAGE], text=True,
    )


def wait_for(description, read, seconds=180):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        value = read()
        if value:
            return value
        time.sleep(2)
    raise RuntimeError(f"Timed out waiting for {description}")


class UpdateServer(SimpleHTTPRequestHandler):
    package_downloads = 0

    def do_GET(self):
        if self.path.endswith(".deb"):
            type(self).package_downloads += 1
        super().do_GET()


class Window:
    def __init__(self, env, log):
        self.session = ""
        self.proc = subprocess.Popen(
            ["WebKitWebDriver", "--host=127.0.0.1", "--port=9515"],
            env=env, stdout=log, stderr=log,
        )

    def command(self, method, path, body=None, timeout=15):
        return json.loads(
            _http(method, "http://127.0.0.1:9515" + path, body, timeout),
        )["value"]

    def start(self):
        def ready():
            if self.proc.poll() is not None:
                raise RuntimeError("WebKitWebDriver exited")
            try:
                return self.command("GET", "/status", timeout=3)
            except RuntimeError:
                return None

        wait_for("WebKitWebDriver", ready, 40)
        session = self.command(
            "POST", "/session",
            {"capabilities": {"alwaysMatch": {
                "webkitgtk:browserOptions": {"binary": EXECUTABLE},
            }}},
            timeout=180,
        )
        self.session = "/session/" + session["sessionId"]
        wait_for("desktop console to render", lambda: self.js("""
            return [...document.querySelectorAll(arguments[0])]
                .some(el => el.getBoundingClientRect().width > 0);
        """, SEL_INPUT.replace(":visible", "")))

    def js(self, script, *args):
        return self.command(
            "POST", self.session + "/execute/sync",
            {"script": script, "args": args},
        )

    def check_update(self):
        return self.command(
            "POST", self.session + "/execute/async",
            {"script": """
                const done = arguments[arguments.length - 1];
                window.__TAURI_INTERNALS__.invoke("check_desktop_update")
                    .then(done, err => done({error: String(err)}));
            """, "args": []},
        )

    def click(self, description, selector, text=""):
        return wait_for(description, lambda: self.js("""
            const item = [...document.querySelectorAll(arguments[0])]
                .find(el => el.getBoundingClientRect().width > 0 &&
                    !el.disabled &&
                    (!arguments[1] || el.textContent.trim() === arguments[1]));
            if (!item) return false;
            item.dispatchEvent(new MouseEvent("click", {
                bubbles: true, cancelable: true, view: window,
            }));
            return true;
        """, selector, text))

    def screenshot(self, label):
        image = self.command("GET", self.session + "/screenshot")
        (ROOT / f"{label}.png").write_bytes(base64.b64decode(image))

    def close(self):
        try:
            if self.session and self.proc.poll() is None:
                self.command("DELETE", self.session, timeout=8)
        except RuntimeError:
            pass
        finally:
            if self.proc.poll() is None:
                self.proc.terminate()
                try:
                    self.proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    self.proc.kill()
                    self.proc.wait()


def desktop_pids():
    pids = set()
    for path in Path("/proc").glob("[0-9]*/exe"):
        try:
            exe = os.readlink(path)
        except OSError:
            continue
        if exe in (EXECUTABLE, EXECUTABLE + " (deleted)"):
            pids.add(int(path.parent.name))
    return pids


def agent_host_pids():
    pids = set()
    for pid in desktop_pids():
        try:
            cmdline = (Path("/proc") / str(pid) / "cmdline").read_bytes()
        except OSError:
            continue
        if b"--agent-host" in cmdline.split(b"\0"):
            pids.add(pid)
    return pids


def stop_desktop():
    for pid in desktop_pids():
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    wait_for("desktop exit", lambda: not desktop_pids(), 30)


def backend_restarted(working):
    try:
        record = json.loads((working / "runtime/shared/runtime.json").read_text())
        info = json.loads(_http("GET", f"http://127.0.0.1:{record['port']}/api/version", timeout=4))
        return info.get("version") == NEW
    except (OSError, ValueError, KeyError, RuntimeError):
        return False


def verify_upgrade(mode):
    working = ROOT / f"{mode}-user-data"
    storage = ROOT / f"{mode}-app-data"
    working.mkdir()
    storage.mkdir()
    env = {
        **os.environ,
        "LANG": "C.UTF-8",
        "TAURI_WEBVIEW_AUTOMATION": "true",
        "WEBKIT_DISABLE_COMPOSITING_MODE": "1",
        "WEBKIT_DISABLE_DMABUF_RENDERER": "1",
        "QWENPAW_WORKING_DIR": str(working),
        "XDG_DATA_HOME": str(storage),
    }
    with (ROOT / f"{mode}.log").open("wb") as log:
        window = Window(env, log)
        try:
            window.start()
            assert installed_version() == BASE
            info = window.check_update()
            assert info and info.get("version") == NEW and info.get("supportsLaterInstall"), info
            print(f"PASS {mode}: installed app detected {NEW} with background download support", flush=True)

            workspace_file = working / "workspaces/default/linux-upgrade-test.txt"
            workspace_file.parent.mkdir(parents=True, exist_ok=True)
            workspace_file.write_text("keep this workspace across a Debian update\n")

            window.click("available update badge", 'span[class*="versionBadgeClickable"]')
            window.screenshot(f"{mode}-available")
            if mode == "cached":
                window.click("Download in Background", "button", "Download in Background")
                window.click("download ready icon", '[class*="appBrandReadyIcon"]')
                window.screenshot("cached-ready")
                assert installed_version() == BASE
                metas = list(storage.rglob("cached-update/update-meta.json"))
                assert len(metas) == 1, metas
                meta = json.loads(metas[0].read_text())
                assert meta["version"] == NEW and meta["platform"] == "linux", meta
                cached = metas[0].parent / meta["artifactFile"]
                assert cached.stat().st_size == (UPDATES / "QwenPaw-Tauri-2.3.1-linux-x86_64.deb").stat().st_size
                downloads = UpdateServer.package_downloads
                print("PASS cached: download completed while the old deb remained installed", flush=True)
                # Reopen the old version to prove the cached state survives relaunch.
                frontend = desktop_pids() - agent_host_pids()
                window.js("void window.__TAURI_INTERNALS__.invoke('quit_app')")
                wait_for("old desktop window to quit", lambda: not (frontend & desktop_pids()), 30)
                window.close()
                window = Window(env, log)
                window.start()
                window.click("persisted download ready icon", '[class*="appBrandReadyIcon"]')
            window.click("Update Now", "button", "Update Now")
            before = desktop_pids()
            window.click(
                "desktop update confirmation",
                'dialog[open][aria-labelledby=desktop-update-title] footer button:last-child',
            )
            wait_for("updated dpkg version", lambda: installed_version() == NEW, 240)
            wait_for("updated desktop process", lambda: (desktop_pids() - before), 150)
            wait_for("updated app backend version", lambda: backend_restarted(working), 180)
            assert workspace_file.read_text() == "keep this workspace across a Debian update\n"
            if mode == "cached":
                assert UpdateServer.package_downloads == downloads, UpdateServer.package_downloads
            print(f"PASS {mode}: dpkg installed {NEW}, app restarted, backend is {NEW}, workspace preserved", flush=True)
        finally:
            window.close()
            stop_desktop()


def main():
    if os.getuid() == 0:
        raise RuntimeError("Run desktop verification as a normal user")
    tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    tls.load_cert_chain(os.environ["UPDATER_TEST_CERT"], os.environ["UPDATER_TEST_TLS_KEY"])
    with ThreadingHTTPServer(("127.0.0.1", 8443), partial(UpdateServer, directory=UPDATES)) as server:
        server.socket = tls.wrap_socket(server.socket, server_side=True)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            manifest = json.loads(_http("GET", "https://localhost:8443/latest.json"))
            assert manifest.get("version") == NEW
            print("PASS: local HTTPS update manifest is reachable", flush=True)
            for mode in ("immediate", "cached"):
                subprocess.run(
                    ["sudo", "apt-get",
                     "-o", "Dir::Etc::sourcelist=" + os.environ["UPDATER_TEST_APT_SOURCES"],
                     "-o", "Dir::Etc::sourceparts=-",
                     "-o", "Acquire::https::Timeout=30",
                     "install", "-y", "--allow-downgrades",
                     "--no-install-recommends", str(UPDATES / "QwenPaw-Tauri-2.3.1b1-linux-x86_64.deb")],
                    check=True,
                )
                verify_upgrade(mode)
        finally:
            server.shutdown()


if __name__ == "__main__":
    main()
