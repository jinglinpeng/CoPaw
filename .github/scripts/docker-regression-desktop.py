"""Real desktop, browser, MCP subprocess and ONNX inference probes."""

import asyncio
import json
from pathlib import Path
import subprocess
import sys

import numpy as np
import onnxruntime
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from playwright.async_api import async_playwright
import psutil


def varint(value):
    result = bytearray()
    while value > 127:
        result.append((value & 127) | 128)
        value >>= 7
    result.append(value)
    return bytes(result)


def field(number, value):
    if isinstance(value, int):
        return varint(number << 3) + varint(value)
    if isinstance(value, str):
        value = value.encode()
    return varint((number << 3) | 2) + varint(len(value)) + value


def infer_identity():
    shape = field(1, field(1, 1)) + field(1, field(1, 2))
    tensor = field(1, 1) + field(2, shape)

    def info(name):
        return field(1, name) + field(2, field(1, tensor))

    node = field(1, "x") + field(2, "y") + field(4, "Identity")
    graph = (
        field(1, node)
        + field(2, "artifact-identity")
        + field(11, info("x"))
        + field(12, info("y"))
    )
    model = field(1, 8) + field(7, graph) + field(8, field(2, 13))
    session = onnxruntime.InferenceSession(
        model, providers=["CPUExecutionProvider"]
    )
    data = np.array([[1.25, -2.5]], dtype=np.float32)
    np.testing.assert_array_equal(session.run(None, {"x": data})[0], data)


async def probe_mcp(command, script):
    params = StdioServerParameters(
        command=command, args=["/app/working/regression-fixtures/" + script]
    )
    async with stdio_client(params) as (reader, writer):
        async with ClientSession(reader, writer) as session:
            await session.initialize()
            assert "artifact_echo" in {
                tool.name for tool in (await session.list_tools()).tools
            }
            response = await session.call_tool(
                "artifact_echo", {"text": "中文 MCP 测试"}
            )
            assert not response.isError
            assert any(
                getattr(block, "text", "") == "中文 MCP 测试"
                for block in response.content
            )


async def main():
    from qwenpaw.agents.tools.desktop_screenshot import desktop_screenshot
    from qwenpaw.config.context import set_current_workspace_dir
    import lark_oapi  # noqa: F401

    infer_identity()
    await asyncio.wait_for(probe_mcp("python", "mcp-python.py"), timeout=45)
    await asyncio.wait_for(probe_mcp("node", "mcp-node.js"), timeout=45)
    screenshots = []
    async with async_playwright() as playwright:
        for headless in (True, False):
            browser = await playwright.chromium.launch(
                executable_path="/usr/bin/chromium",
                headless=headless,
                args=["--no-sandbox", "--disable-dev-shm-usage"],
            )
            try:
                page = await browser.new_page()
                errors = []
                console_errors = []
                page.on("pageerror", lambda error: errors.append(str(error)))
                page.on(
                    "console",
                    lambda message: console_errors.append(message.text)
                    if message.type == "error"
                    else None,
                )
                response = await page.goto(
                    "http://127.0.0.1:8088/chat", wait_until="networkidle"
                )
                assert response.status == 200
                await page.wait_for_function(
                    "document.body.innerText.trim().length > 0", timeout=30000
                )
                assert not errors, errors
                mode = "headless" if headless else "headed"
                shot = "/app/working/artifact-console-" + mode + ".png"
                await page.screenshot(path=shot, full_page=True)
                screenshots.append(shot)
                await page.set_content(
                    '<input aria-label="Value"><button onclick="'
                    "document.querySelector('p').innerText="
                    "document.querySelector('input').value"
                    '">Apply</button><p>Waiting</p>'
                )
                await page.get_by_role("textbox", name="Value").fill("中文 OK")
                await page.get_by_role("button", name="Apply").click()
                assert await page.locator("p").inner_text() == "中文 OK"
            except Exception:
                failure = "/app/working/artifact-console-failure.png"
                await page.screenshot(path=failure, full_page=True)
                print(
                    json.dumps(
                        {
                            "url": page.url,
                            "page_errors": errors,
                            "console_errors": console_errors,
                            "body": await page.locator("body").inner_text(),
                        }
                    ),
                    file=sys.stderr,
                    flush=True,
                )
                raise
            finally:
                await browser.close()
    set_current_workspace_dir(Path("/app/working/workspaces/default"))
    desktop_path = "/app/working/artifact-desktop.png"
    desktop = await desktop_screenshot(path=desktop_path)
    assert any(
        '"ok": true' in getattr(block, "text", "") for block in desktop.content
    )
    assert Path(desktop_path).stat().st_size > 1000
    screenshots.append(desktop_path)
    names = {process.info["name"] for process in psutil.process_iter(["name"])}
    assert {"Xvfb", "xfce4-session", "dbus-daemon"} <= names, names
    system_python = subprocess.check_output(
        [
            "/usr/bin/python3",
            "-c",
            "import sys,ssl,json; print(json.dumps({"
            "'python':sys.version.split()[0],"
            "'openssl':ssl.OPENSSL_VERSION}))",
        ],
        text=True,
    )
    print(
        json.dumps(
            {
                "onnx_identity_inference": "passed",
                "stdio_mcp_python": "passed",
                "stdio_mcp_node": "passed",
                "feishu_sdk_import": "passed",
                "console_headless_and_headed": "passed",
                "desktop_services": "passed",
                "system_python": json.loads(system_python),
                "screenshots": screenshots,
            }
        ),
        flush=True,
    )


if __name__ == "__main__":
    asyncio.run(main())
