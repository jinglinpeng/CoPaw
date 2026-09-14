"""Architecture-sensitive probes; no credentials or model calls."""

import asyncio
import bz2
import ctypes
import importlib.metadata
import io
import json
import lzma
import os
import platform
from pathlib import Path
import shlex
import sqlite3
import ssl
import subprocess
import sys
import urllib.request
import zlib

import numpy as np
import onnxruntime
import orjson
import psutil
from PIL import Image
from cryptography.hazmat.primitives.ciphers.aead import AESGCM


async def probe_tools():
    from agentscope.message import ToolResultState
    from qwenpaw.agents.tools.browser import browser
    from qwenpaw.agents.tools.file_io import read_file, write_file
    from qwenpaw.agents.tools.shell import execute_shell_command
    from qwenpaw.browser.execution.kernel import get_default_kernel_manager
    from qwenpaw.config.context import (
        set_current_session_id,
        set_current_workspace_dir,
    )

    def text_of(chunk):
        return "\n".join(getattr(block, "text", "") for block in chunk.content)

    workspace = Path("/app/working/workspaces/default")
    set_current_workspace_dir(workspace)
    set_current_session_id("docker-artifacts-smoke")
    marker = workspace / "docker-artifacts-smoke.md"
    await write_file(str(marker), "中文文件工具测试")
    assert "中文文件工具测试" in text_of(await read_file(str(marker)))
    python_probe = "import sys,ssl; print(sys.executable, ssl.OPENSSL_VERSION)"
    shell = await execute_shell_command(
        "python -c " + shlex.quote(python_probe),
        timeout=20,
    )
    assert "/app/venv/bin/python" in text_of(shell), text_of(shell)
    assert "OpenSSL 3.5." in text_of(shell), text_of(shell)
    code = """
browser = await Browser.connect(identity="guest")
page = await browser.open("http://127.0.0.1:8088/api/version")
obs = await page.snapshot()
assert "version" in obs.text, obs.text
shot = await page.screenshot()
await browser.close()
return shot["path"]
"""
    try:
        result = await browser(code=code)
        assert result.state == ToolResultState.SUCCESS, text_of(result)
        screenshot = text_of(result).strip()
        assert Path(screenshot).stat().st_size > 1000, screenshot
    finally:
        await get_default_kernel_manager().discard_all_workers()
    with urllib.request.urlopen(
        "https://pypi.org/pypi/pyfiglet/1.0.4/json", timeout=15
    ) as response:
        assert response.status == 200
    return {
        "file_tools": "passed",
        "shell_tool": "passed",
        "browser_tool": "passed",
        "browser_screenshot": screenshot,
        "https_certificate_validation": "passed",
    }


def main():
    payload = "中文原生扩展兼容性测试".encode("utf-8")
    for codec in (bz2, lzma, zlib):
        assert codec.decompress(codec.compress(payload)) == payload
    ctypes.CDLL(None)
    db = sqlite3.connect(":memory:")
    db.execute("create table smoke (value text)")
    db.execute("insert into smoke values (?)", (payload.decode("utf-8"),))
    stored = db.execute("select value from smoke").fetchone()[0]
    assert stored.encode("utf-8") == payload
    db.close()
    image = Image.new("RGB", (12, 12), (12, 34, 56))
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    buffer.seek(0)
    assert Image.open(buffer).getpixel((0, 0)) == (12, 34, 56)
    aes = AESGCM(AESGCM.generate_key(bit_length=128))
    nonce = os.urandom(12)
    encrypted = aes.encrypt(nonce, payload, None)
    assert aes.decrypt(nonce, encrypted, None) == payload
    assert (np.array([1, 2, 3]) @ np.array([2, 3, 4])) == 20
    assert orjson.loads(orjson.dumps({"中文": [1, 2]})) == {"中文": [1, 2]}
    assert psutil.Process(os.getpid()).is_running()
    assert onnxruntime.get_available_providers()
    onnxruntime.SessionOptions()

    packages = (
        "numpy",
        "onnxruntime",
        "orjson",
        "psutil",
        "Pillow",
        "cryptography",
    )
    versions = {name: importlib.metadata.version(name) for name in packages}
    node = subprocess.check_output(["node", "--version"], text=True).strip()
    print(
        json.dumps(
            {
                "machine": platform.machine(),
                "python": sys.version.split()[0],
                "executable": sys.executable,
                "openssl": ssl.OPENSSL_VERSION,
                "node": node,
                "sqlite": sqlite3.sqlite_version,
                "dependencies": versions,
                "native_probes": "passed",
                "functional_probes": asyncio.run(probe_tools()),
            },
            indent=2,
        ),
        flush=True,
    )


if __name__ == "__main__":
    main()
