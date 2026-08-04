# -*- coding: utf-8 -*-
"""Unit tests for the legacy pywebview desktop bridge."""

import io
import types
import urllib.request

from qwenpaw.cli import desktop_cmd


class _Response(io.BytesIO):
    def __enter__(self) -> "_Response":
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()


class _EventHook:
    def __init__(self) -> None:
        self.handlers = []

    def __iadd__(self, handler):
        self.handlers.append(handler)
        return self

    def fire(self, sender, args) -> None:
        for handler in list(self.handlers):
            handler(sender, args)


class _FakeEnvironment:
    def __init__(self) -> None:
        self.BrowserProcessExited = _EventHook()


class _FakeCore:
    def __init__(self) -> None:
        self.ProcessFailed = _EventHook()
        self.Environment = _FakeEnvironment()
        self.reload_count = 0

    def Reload(self) -> None:
        self.reload_count += 1


class _FakeControl:
    def __init__(self, core: _FakeCore | None = None) -> None:
        self.CoreWebView2 = core or _FakeCore()
        self.CoreWebView2InitializationCompleted = _EventHook()
        self.disposed = False

    def Dispose(self) -> None:
        self.disposed = True


class _FakeControls:
    def __init__(self) -> None:
        self.added = []
        self.removed = []

    def Add(self, control) -> None:
        self.added.append(control)

    def Remove(self, control) -> None:
        self.removed.append(control)


def test_save_file_passes_headers_to_download_request(
    monkeypatch,
    tmp_path,
) -> None:
    destination = tmp_path / "backup.zip"
    monkeypatch.setattr(
        desktop_cmd,
        "webview",
        types.SimpleNamespace(
            SAVE_DIALOG=1,
            windows=[
                types.SimpleNamespace(
                    create_file_dialog=lambda *_args, **_kwargs: str(
                        destination,
                    ),
                ),
            ],
        ),
    )

    captured_url = ""
    captured_headers: dict[str, str] = {}

    def fake_urlopen(request: urllib.request.Request) -> _Response:
        nonlocal captured_headers, captured_url
        captured_url = request.full_url
        captured_headers = {
            key.lower(): value for key, value in request.header_items()
        }
        return _Response(b"zip")

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    saved = desktop_cmd.WebViewAPI().save_file(
        "http://127.0.0.1:43123/api/backups/abc/export",
        "backup.zip",
        {"Authorization": "Bearer tok", "X-Agent-Id": "agent-a"},
    )

    assert saved is True
    assert destination.read_bytes() == b"zip"
    assert captured_url == "http://127.0.0.1:43123/api/backups/abc/export"
    assert captured_headers["authorization"] == "Bearer tok"
    assert captured_headers["x-agent-id"] == "agent-a"


def test_webview2_recovery_reloads_renderer_and_recreates_browser(
    monkeypatch,
) -> None:
    monkeypatch.setattr(desktop_cmd.sys, "platform", "win32")
    edge_module = types.ModuleType("webview.platforms.edgechromium")

    class FakeEdgeChrome:
        def __init__(self, form, window, cache_dir: str) -> None:
            self.form = form
            self.pywebview_window = window
            self.user_data_folder = cache_dir
            self.webview = _FakeControl()
            form.Controls.Add(self.webview)

        def on_webview_ready(self, _sender, _args) -> None:
            pass

    edge_module.EdgeChrome = FakeEdgeChrome
    monkeypatch.setitem(
        desktop_cmd.sys.modules,
        "webview.platforms.edgechromium",
        edge_module,
    )
    desktop_cmd._install_webview2_recovery(  # pylint: disable=protected-access
        "http://127.0.0.1:43123",
    )

    form = types.SimpleNamespace(Controls=_FakeControls())
    old_browser = FakeEdgeChrome(form, object(), "webview-data")
    form.browser = old_browser
    form.webview = old_browser.webview
    old_core = old_browser.webview.CoreWebView2
    old_browser.on_webview_ready(
        old_browser.webview,
        types.SimpleNamespace(IsSuccess=True),
    )

    old_core.ProcessFailed.fire(
        old_core,
        types.SimpleNamespace(
            ProcessFailedKind="RenderProcessExited",
            Reason="Unexpected",
        ),
    )
    assert old_core.reload_count == 1

    old_core.ProcessFailed.fire(
        old_core,
        types.SimpleNamespace(
            ProcessFailedKind="BrowserProcessExited",
            Reason="Unexpected",
        ),
    )
    assert form.browser is old_browser

    old_core.Environment.BrowserProcessExited.fire(
        old_core.Environment,
        types.SimpleNamespace(BrowserProcessExitKind="Failed"),
    )

    assert form.browser is not old_browser
    assert old_browser.webview.disposed is True
    assert form.Controls.removed == [old_browser.webview]
