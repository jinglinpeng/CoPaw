# -*- coding: utf-8 -*-
"""Check the packaged native window before running the shared web/API checks."""

import base64
import json
import os
from pathlib import Path
import subprocess
import sys
import time

from desktop_verify import SEL_INPUT, _http


def main() -> int:
    artifacts = list(Path("dist").glob("QwenPaw-Tauri-*-linux-*.AppImage"))
    if len(artifacts) != 1:
        raise RuntimeError(f"Expected one AppImage, found {len(artifacts)}")
    log_dir = Path(os.environ.get("RUNNER_TEMP", "/tmp"))
    screenshots = log_dir / "verify-screenshots"
    screenshots.mkdir(parents=True, exist_ok=True)
    endpoint = "http://127.0.0.1:9515"
    session = ""

    def command(method, path, body=None, timeout=30):
        return json.loads(_http(method, endpoint + path, body, timeout))[
            "value"
        ]

    env = {
        **os.environ,
        "APPIMAGE_EXTRACT_AND_RUN": "1",
        "TAURI_WEBVIEW_AUTOMATION": "true",
        "WEBKIT_DISABLE_COMPOSITING_MODE": "1",
        "WEBKIT_DISABLE_DMABUF_RENDERER": "1",
    }
    with (
        (log_dir / "qwenpaw-desktop-stdout.log").open("w") as stdout,
        (log_dir / "qwenpaw-desktop-stderr.log").open("w") as stderr,
    ):
        driver = subprocess.Popen(
            ["WebKitWebDriver", "--host=127.0.0.1", "--port=9515"],
            env=env,
            stdout=stdout,
            stderr=stderr,
        )
        try:
            for _ in range(30):
                if driver.poll() is not None:
                    raise RuntimeError("WebKitWebDriver exited during startup")
                try:
                    command("GET", "/status", timeout=2)
                    break
                except RuntimeError:
                    time.sleep(1)
            else:
                raise RuntimeError("WebKitWebDriver did not start within 30s")
            value = command(
                "POST",
                "/session",
                {
                    "capabilities": {
                        "alwaysMatch": {
                            "webkitgtk:browserOptions": {
                                "binary": str(artifacts[0].resolve()),
                            },
                        },
                    },
                },
                timeout=180,
            )
            session = "/session/" + value["sessionId"]
            # Do not navigate: the packaged window must load the console itself.
            # CSS :visible is a Playwright extension, so check geometry here.
            script = """
                const input = [...document.querySelectorAll(arguments[0])]
                  .find(el => el.getBoundingClientRect().width > 0 &&
                              el.getBoundingClientRect().height > 0 &&
                              getComputedStyle(el).visibility === 'visible');
                return input ? location.origin : null;
            """
            base_url = None
            for _ in range(90):
                base_url = command(
                    "POST",
                    session + "/execute/sync",
                    {
                        "script": script,
                        "args": [SEL_INPUT.replace(":visible", "")],
                    },
                )
                if base_url:
                    break
                time.sleep(2)
            if not base_url or not base_url.startswith("http://127.0.0.1:"):
                raise RuntimeError(
                    "Native window did not render the chat input"
                )
            screenshot = command("GET", session + "/screenshot")
            (screenshots / "00-native-window.png").write_bytes(
                base64.b64decode(screenshot),
            )
            print(
                f"PASS  packaged native window rendered at {base_url}",
                flush=True,
            )
            bootstrap = (
                Path.home() / ".qwenpaw/workspaces/default/BOOTSTRAP.md"
            )
            bootstrap.unlink(missing_ok=True)
            return subprocess.run(
                [
                    sys.executable,
                    str(Path(__file__).with_name("desktop_verify.py")),
                    "--base-url",
                    base_url,
                    "--ui-mode",
                    "tauri-linux",
                    "--headed",
                    *sys.argv[1:],
                ],
                check=False,
            ).returncode
        finally:
            if session:
                try:
                    command("DELETE", session, timeout=10)
                except RuntimeError as exc:
                    print(
                        f"WARN  closing native window: {exc}", file=sys.stderr
                    )
            driver.terminate()
            try:
                driver.wait(timeout=10)
            except subprocess.TimeoutExpired:
                driver.kill()
                driver.wait()


if __name__ == "__main__":
    sys.exit(main())
