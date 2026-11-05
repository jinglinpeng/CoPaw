# -*- coding: utf-8 -*-
"""Validation-branch-only native window restart checks; no LLM or product API.

Attach to the installed Tauri WebView2 via CDP. Win32 changes ordinary geometry
and min/max state; existing Tauri tray commands hide and gracefully quit. Never
kill a process, set TOPMOST, patch the app, or read application credentials.
"""
from __future__ import annotations

import argparse
import ctypes
from contextlib import ExitStack
from ctypes import wintypes
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import urllib.error
import urllib.request


class Placement(ctypes.Structure):
    _fields_ = [
        ("length", wintypes.UINT),
        ("flags", wintypes.UINT),
        ("showCmd", wintypes.UINT),
        ("ptMinPosition", wintypes.POINT),
        ("ptMaxPosition", wintypes.POINT),
        ("rcNormalPosition", wintypes.RECT),
    ]


def rectangle(rect):
    return [rect.left, rect.top, rect.right, rect.bottom]


class NativeWindow:
    """Only the Win32 operations needed by this issue's smoke scenario."""

    def __init__(self):
        self.user = ctypes.WinDLL("user32", use_last_error=True)
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self.callback_type = ctypes.WINFUNCTYPE(
            wintypes.BOOL,
            wintypes.HWND,
            wintypes.LPARAM,
        )
        signatures = {
            "EnumWindows": (
                [self.callback_type, wintypes.LPARAM],
                wintypes.BOOL,
            ),
            "GetWindowThreadProcessId": (
                [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)],
                wintypes.DWORD,
            ),
            "GetWindowTextW": (
                [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int],
                ctypes.c_int,
            ),
            "GetWindow": ([wintypes.HWND, wintypes.UINT], wintypes.HWND),
            "GetWindowRect": (
                [wintypes.HWND, ctypes.POINTER(wintypes.RECT)],
                wintypes.BOOL,
            ),
            "GetClientRect": (
                [wintypes.HWND, ctypes.POINTER(wintypes.RECT)],
                wintypes.BOOL,
            ),
            "GetWindowPlacement": (
                [wintypes.HWND, ctypes.POINTER(Placement)],
                wintypes.BOOL,
            ),
            "IsWindowVisible": ([wintypes.HWND], wintypes.BOOL),
            "IsIconic": ([wintypes.HWND], wintypes.BOOL),
            "IsZoomed": ([wintypes.HWND], wintypes.BOOL),
            "GetDpiForWindow": ([wintypes.HWND], wintypes.UINT),
            "GetWindowLongPtrW": (
                [wintypes.HWND, ctypes.c_int],
                ctypes.c_ssize_t,
            ),
            "ShowWindow": ([wintypes.HWND, ctypes.c_int], wintypes.BOOL),
            "SetWindowPos": (
                [
                    wintypes.HWND,
                    wintypes.HWND,
                    ctypes.c_int,
                    ctypes.c_int,
                    ctypes.c_int,
                    ctypes.c_int,
                    wintypes.UINT,
                ],
                wintypes.BOOL,
            ),
            "SystemParametersInfoW": (
                [wintypes.UINT, wintypes.UINT, ctypes.c_void_p, wintypes.UINT],
                wintypes.BOOL,
            ),
            "SetProcessDpiAwarenessContext": (
                [ctypes.c_void_p],
                wintypes.BOOL,
            ),
            "GetThreadDpiAwarenessContext": ([], ctypes.c_void_p),
            "GetAwarenessFromDpiAwarenessContext": (
                [ctypes.c_void_p],
                ctypes.c_int,
            ),
        }
        for name, (arguments, result) in signatures.items():
            function = getattr(self.user, name)
            function.argtypes, function.restype = arguments, result
        for name, arguments, result in [
            (
                "OpenProcess",
                [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD],
                wintypes.HANDLE,
            ),
            (
                "WaitForSingleObject",
                [wintypes.HANDLE, wintypes.DWORD],
                wintypes.DWORD,
            ),
            (
                "GetExitCodeProcess",
                [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)],
                wintypes.BOOL,
            ),
            (
                "QueryFullProcessImageNameW",
                [
                    wintypes.HANDLE,
                    wintypes.DWORD,
                    wintypes.LPWSTR,
                    ctypes.POINTER(wintypes.DWORD),
                ],
                wintypes.BOOL,
            ),
            ("CloseHandle", [wintypes.HANDLE], wintypes.BOOL),
        ]:
            function = getattr(self.kernel, name)
            function.argtypes, function.restype = arguments, result
        # Physical coordinates throughout; otherwise Python's DPI virtualization
        # can make a correct Tauri logical/physical restore look incorrect.
        self.dpi_aware = bool(
            self.user.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)),
        )
        awareness = self.user.GetAwarenessFromDpiAwarenessContext(
            self.user.GetThreadDpiAwarenessContext(),
        )
        if not self.dpi_aware and awareness != 2:
            raise RuntimeError(
                "Smoke process cannot use physical per-monitor DPI coordinates",
            )

    def check(self, success):
        if not success:
            raise ctypes.WinError(ctypes.get_last_error())

    def find(self, pid):
        windows = []

        @self.callback_type
        def visit(hwnd, _):
            owner = wintypes.DWORD(0)
            self.user.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
            title = ctypes.create_unicode_buffer(512)
            self.user.GetWindowTextW(hwnd, title, len(title))
            if (
                owner.value == pid
                and not self.user.GetWindow(hwnd, 4)
                and "QwenPaw" in title.value
            ):
                windows.append(hwnd)
            return True

        self.check(self.user.EnumWindows(visit, 0))
        return windows[0] if windows else None

    def snapshot(self, pid, hwnd):
        outer, client = wintypes.RECT(), wintypes.RECT()
        placement = Placement(length=ctypes.sizeof(Placement))
        self.check(self.user.GetWindowRect(hwnd, ctypes.byref(outer)))
        self.check(self.user.GetClientRect(hwnd, ctypes.byref(client)))
        self.check(self.user.GetWindowPlacement(hwnd, ctypes.byref(placement)))
        exstyle = self.user.GetWindowLongPtrW(hwnd, -20) & 0xFFFFFFFF
        return {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "pid": pid,
            "hwnd": int(hwnd),
            "outer_rect": rectangle(outer),
            "client_rect": rectangle(client),
            "dpi": self.user.GetDpiForWindow(hwnd),
            "visible": bool(self.user.IsWindowVisible(hwnd)),
            "minimized": bool(self.user.IsIconic(hwnd)),
            "maximized": bool(self.user.IsZoomed(hwnd)),
            "placement_show_cmd": placement.showCmd,
            "placement_normal_rect": rectangle(placement.rcNormalPosition),
            "exstyle": exstyle,
            "exstyle_hex": hex(exstyle),
            "ws_ex_topmost": bool(exstyle & 0x8),
        }

    def work_area(self):
        area = wintypes.RECT()
        self.check(
            self.user.SystemParametersInfoW(0x30, 0, ctypes.byref(area), 0),
        )
        return rectangle(area)

    def open_process(self, pid):
        handle = self.kernel.OpenProcess(0x100000 | 0x1000, False, pid)
        self.check(handle)
        return handle

    def process_image(self, pid):
        handle = self.open_process(pid)
        try:
            size = wintypes.DWORD(32768)
            name = ctypes.create_unicode_buffer(size.value)
            self.check(
                self.kernel.QueryFullProcessImageNameW(
                    handle,
                    0,
                    name,
                    ctypes.byref(size),
                ),
            )
            return Path(name.value).resolve()
        finally:
            self.kernel.CloseHandle(handle)


def poll(description, sample, predicate, timeout=90):
    deadline = time.monotonic() + timeout
    latest = None
    while time.monotonic() < deadline:
        try:
            latest = sample()
            if predicate(latest):
                return latest
        except (OSError, urllib.error.URLError) as error:
            latest = str(error)
        time.sleep(0.25)
    raise AssertionError(f"Timed out: {description}; last={latest}")


class Smoke:
    def __init__(self, args, playwright):
        self.args, self.playwright = args, playwright
        self.native = NativeWindow()
        self.pid, self.page = args.pid, None
        self.events, self.assertions = [], []
        self.resources = ExitStack()
        args.output.mkdir(parents=True, exist_ok=True)

    def record(self, event, **details):
        item = {
            "event": event,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            **details,
        }
        self.events.append(item)
        print(json.dumps(item), flush=True)
        (self.args.output / "result.json").write_text(
            json.dumps(
                {"events": self.events, "assertions": self.assertions},
                indent=2,
            ),
        )

    def require(self, condition, message):
        self.assertions.append(
            {"assertion": message, "passed": bool(condition)},
        )
        if not condition:
            raise AssertionError(message)
        self.record("assertion_passed", assertion=message)

    def capture(self, phase):
        hwnd = poll(
            "native main window",
            lambda: self.native.find(self.pid),
            bool,
        )
        snapshot = self.native.snapshot(self.pid, hwnd)
        self.record(phase, **snapshot)
        return snapshot

    def connect(self):
        self.require(
            self.native.process_image(self.pid) == self.args.exe.resolve(),
            "PID belongs to the installed executable",
        )

        def cdp_ready():
            with urllib.request.urlopen(
                self.args.cdp_url + "/json/version",
                timeout=2,
            ) as response:
                return response.read()

        poll("CDP endpoint", cdp_ready, bool)
        browser = self.playwright.chromium.connect_over_cdp(
            self.args.cdp_url,
            timeout=30000,
        )
        from playwright.sync_api import Error as PlaywrightError

        def native_page():
            for context in browser.contexts:
                for page in context.pages:
                    try:
                        if not page.is_closed() and page.evaluate(
                            "typeof window.__TAURI_INTERNALS__?.invoke === 'function'",
                        ):
                            return page
                    except PlaywrightError:
                        # Real startup navigation can replace the JS context.
                        continue
            return None

        self.page = poll(
            "actual WebView2 page with existing Tauri IPC",
            native_page,
            bool,
        )
        self.record(
            "cdp_attached",
            pid=self.pid,
            url=self.page.url,
            bridge="existing @tauri-apps/api/core invoke bridge; withGlobalTauri=false",
        )
        port_file = self.args.working_dir / "desktop_port"

        def version():
            port = int(port_file.read_text().strip())
            with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/api/version",
                timeout=2,
            ) as response:
                return json.load(response).get("version")

        self.record(
            "backend_version",
            version=poll("packaged backend readiness", version, bool),
        )

    def invoke(self, command):
        # This is exactly the bridge used by the already bundled core.invoke;
        # no global API injection, remote product API, or debug command.
        self.page.evaluate(
            """command => {
                window.__windowSmokeError = null;
                window.__TAURI_INTERNALS__.invoke(command).catch(error => {
                    window.__windowSmokeError = String(error);
                });
            }""",
            command,
        )

    def quit(self, phase):
        handle = self.native.open_process(self.pid)
        try:
            self.capture(phase + "_before_accepted_quit")
            try:
                self.invoke("quit_app")
            except Exception as error:
                # CDP may disconnect as the successful quit destroys WebView2.
                # The native process exit/code below is still mandatory.
                self.record("quit_cdp_result", detail=str(error))
            status = self.native.kernel.WaitForSingleObject(handle, 45000)
            self.require(
                status == 0,
                f"{phase}: accepted quit exits old PID within45s without kill",
            )
            code = wintypes.DWORD(0)
            self.native.check(
                self.native.kernel.GetExitCodeProcess(
                    handle,
                    ctypes.byref(code),
                ),
            )
            self.require(
                code.value == 0,
                f"{phase}: native process exits successfully",
            )
            self.record("graceful_exit", pid=self.pid, exit_code=code.value)
        finally:
            self.native.kernel.CloseHandle(handle)

        def cdp_closed():
            try:
                with urllib.request.urlopen(
                    self.args.cdp_url + "/json/version",
                    timeout=1,
                ):
                    return False
            except (OSError, urllib.error.URLError):
                return True

        poll(
            "old CDP endpoint closes before restart",
            cdp_closed,
            bool,
            timeout=15,
        )

    def restart(self, phase):
        stdout = self.resources.enter_context(
            (self.args.output / f"{phase}-stdout.log").open("w"),
        )
        stderr = self.resources.enter_context(
            (self.args.output / f"{phase}-stderr.log").open("w"),
        )
        env = {
            **os.environ,
            "QWENPAW_WORKING_DIR": str(self.args.working_dir),
            "WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS": "--remote-debugging-port="
            + self.args.cdp_url.rsplit(":", 1)[1],
        }
        # Failure cleanup belongs to the existing workflow finalizer; a Popen
        # context manager would wait without bound after a failed graceful quit.
        process = subprocess.Popen(  # pylint: disable=consider-using-with
            [str(self.args.exe)],
            env=env,
            stdout=stdout,
            stderr=stderr,
        )
        self.pid = process.pid
        self.record(
            "relaunch_installed_application",
            pid=self.pid,
            phase=phase,
        )
        self.connect()
        return self.capture(phase)

    def settled(self, phase, predicate):
        def sample():
            hwnd = self.native.find(self.pid)
            return self.native.snapshot(self.pid, hwnd) if hwnd else None

        snapshot = poll(
            phase,
            sample,
            lambda state: state is not None and predicate(state),
            timeout=20,
        )
        snapshot = dict(snapshot)
        self.record(phase, **snapshot)
        return snapshot

    def same_normal(self, expected, phase):
        def matches(state):
            return (
                state["visible"]
                and not state["minimized"]
                and not state["maximized"]
                and all(
                    abs(a - b) <= 4
                    for key in ("outer_rect", "client_rect")
                    for a, b in zip(state[key], expected[key])
                )
            )

        observed = self.settled(phase, matches)
        self.require(
            observed["visible"]
            and not observed["minimized"]
            and not observed["maximized"],
            phase + ": visible ordinary window",
        )
        for key in ("outer_rect", "client_rect"):
            self.require(
                all(
                    abs(a - b) <= 4
                    for a, b in zip(observed[key], expected[key])
                ),
                phase + ": restores " + key + " within4 physical pixels",
            )
        self.require(
            observed["dpi"] == expected["dpi"],
            phase + ": same runner DPI",
        )

    def run(self):
        self.connect()
        initial = self.capture("initial_native_window")
        hwnd = initial["hwnd"]
        self.native.user.ShowWindow(
            hwnd,
            9,
        )  # SW_RESTORE, including tray hidden
        ordinary = self.settled(
            "initial_normalized",
            lambda state: state["visible"]
            and not state["minimized"]
            and not state["maximized"],
        )
        left, top, right, bottom = self.native.work_area()
        frame_width = (
            ordinary["outer_rect"][2]
            - ordinary["outer_rect"][0]
            - ordinary["client_rect"][2]
        )
        frame_height = (
            ordinary["outer_rect"][3]
            - ordinary["outer_rect"][1]
            - ordinary["client_rect"][3]
        )
        min_width = round(960 * ordinary["dpi"] / 96) + frame_width
        min_height = round(600 * ordinary["dpi"] / 96) + frame_height
        width = min(max(1120, min_width), right - left - 32)
        height = min(max(740, min_height), bottom - top - 16)
        self.require(
            width >= min_width and height >= min_height,
            "runner work area can fit configured normal minimum at current DPI",
        )
        self.native.check(
            self.native.user.SetWindowPos(
                hwnd,
                None,
                left + 16,
                top + 8,
                width,
                height,
                0x4 | 0x10,
            ),
        )  # NOZORDER|NOACTIVATE; never TOPMOST
        target = [left + 16, top + 8, left + 16 + width, top + 8 + height]
        self.record(
            "requested_normal_rect",
            target=target,
            work_area=[left, top, right, bottom],
        )
        expected = self.settled(
            "normal_geometry_before_exit",
            lambda state: state["visible"]
            and not state["minimized"]
            and not state["maximized"]
            and all(
                abs(a - b) <= 4 for a, b in zip(state["outer_rect"], target)
            ),
        )
        self.require(
            all(
                abs(a - b) <= 4 for a, b in zip(expected["outer_rect"], target)
            ),
            "SetWindowPos reached requested physical rectangle within4 pixels",
        )
        self.require(
            expected["visible"]
            and not expected["minimized"]
            and not expected["maximized"],
            "Win32 geometry setup yields ordinary window",
        )
        self.quit("normal")
        self.restart("normal_restored")
        self.same_normal(expected, "normal restart")

        hwnd = self.native.find(self.pid)
        self.native.user.ShowWindow(hwnd, 3)  # SW_MAXIMIZE
        poll("maximize", lambda: bool(self.native.user.IsZoomed(hwnd)), bool)
        self.capture("maximized_before_exit")
        self.quit("maximized")
        self.restart("maximized_restored")
        maximized = self.settled(
            "maximized_restored_settled",
            lambda state: state["visible"]
            and state["maximized"]
            and not state["minimized"],
        )
        self.require(
            maximized["visible"]
            and maximized["maximized"]
            and not maximized["minimized"],
            "restart restores maximized state",
        )
        self.native.user.ShowWindow(maximized["hwnd"], 9)
        self.same_normal(expected, "unmaximize after restart")

        self.native.user.ShowWindow(
            self.native.find(self.pid),
            6,
        )  # SW_MINIMIZE
        poll(
            "minimize",
            lambda: bool(
                self.native.user.IsIconic(self.native.find(self.pid)),
            ),
            bool,
        )
        self.capture("minimized_before_exit")
        self.quit("minimized")
        self.restart("after_minimized_exit")
        self.same_normal(expected, "minimized state is transient")

        self.invoke("minimize_to_tray")
        poll(
            "tray hide",
            lambda: bool(
                self.native.user.IsWindowVisible(self.native.find(self.pid)),
            ),
            lambda visible: not visible,
        )
        self.capture("tray_hidden_before_exit")
        self.quit("tray_hidden")
        self.restart("after_hidden_exit")
        self.same_normal(expected, "hidden state is transient")
        self.quit("final_cleanup")
        self.record(
            "smoke_completed",
            result="passed",
            topmost_scope=(
                "read-only phase observations; no TOPMOST changes "
                "or unknown8560 trigger tested"
            ),
        )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--exe", type=Path, required=True)
    parser.add_argument("--pid", type=int, required=True)
    parser.add_argument("--working-dir", type=Path, required=True)
    parser.add_argument("--cdp-url", default="http://127.0.0.1:9222")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if sys.platform != "win32":
        parser.error(
            "This task-specific smoke must run on the native Windows runner",
        )
    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        smoke = Smoke(args, playwright)
        try:
            smoke.run()
        except Exception as error:
            smoke.record("smoke_failed", error=str(error))
            raise
        finally:
            smoke.resources.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
