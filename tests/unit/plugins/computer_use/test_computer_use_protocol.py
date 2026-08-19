# -*- coding: utf-8 -*-
"""Tests for the thin Computer Use protocol adapter."""

# Tests reach into module internals to pin the protocol contract, and their
# fakes deliberately accept arguments they ignore to match real signatures.
# pylint: disable=protected-access, unused-argument, unnecessary-lambda
# pylint: disable=useless-return, use-implicit-booleaness-not-comparison

from __future__ import annotations

from collections.abc import Iterator, Mapping
import importlib
import json
import socket
import threading
from typing import Any

import pytest

from agentscope.message import ToolResultState
from agentscope.tool import FunctionTool, ToolChunk
import computer_use.client as client_module
import computer_use.dispatch as dispatch_module
from computer_use.client import ComputerUseClient
from computer_use.dispatch import (
    _element_line,
    _error,
    _native_request,
    _response,
    _with_compact_elements,
    computer_use,
)
from computer_use.input_macos import normalize_key as normalize_macos_key
from computer_use.input_windows import normalize_key as normalize_windows_key
from computer_use.protocol import ComputerUseProtocolError
from computer_use.transport.base import (
    ComputerUseTransport,
    ReverseRequestHandler,
)
from qwenpaw.app.computer_use import (
    HostRuntimeProvider,
    set_current_computer_use_turn_id,
)
from qwenpaw.app.computer_use import runtime as runtime_module


@pytest.fixture(autouse=True)
def _reset_host_runtime(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    for name in (
        "QWENPAW_COMPUTER_USE_PIPE",
        "QWENPAW_COMPUTER_USE_CAPABILITY",
        "QWENPAW_COMPUTER_USE_PROTOCOL",
        "QWENPAW_COMPUTER_USE_CONTROL_HOST",
        "QWENPAW_COMPUTER_USE_CONTROL_PORT",
        "QWENPAW_COMPUTER_USE_CONTROL_TOKEN",
    ):
        monkeypatch.delenv(name, raising=False)
    HostRuntimeProvider._capability = None
    yield
    HostRuntimeProvider._capability = None


def test_host_runtime_requests_a_capability_only_when_needed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(runtime_module.sys, "platform", "darwin")
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]
    token = "test-token"
    received: dict[str, object] = {}

    def _serve_once() -> None:
        with listener:
            connection, _ = listener.accept()
            with connection, connection.makefile("rwb") as stream:
                received.update(json.loads(stream.readline()))
                stream.write(
                    b'{"ok":true,"pipe_name":"pipe-1",'
                    b'"capability":"secret-1"}\n',
                )
                stream.flush()

    server = threading.Thread(target=_serve_once)
    server.start()
    monkeypatch.setenv("QWENPAW_COMPUTER_USE_CONTROL_HOST", "127.0.0.1")
    monkeypatch.setenv("QWENPAW_COMPUTER_USE_CONTROL_PORT", str(port))
    monkeypatch.setenv("QWENPAW_COMPUTER_USE_CONTROL_TOKEN", token)

    assert HostRuntimeProvider.is_available() is True
    assert received == {}
    capability = HostRuntimeProvider.acquire_capability()
    server.join(timeout=1)

    assert capability == runtime_module.RuntimeCapability(
        "pipe-1",
        "secret-1",
        runtime_module.COMPUTER_USE_PROTOCOL_VERSION,
    )
    assert received == {
        "token": token,
        "action": "acquire",
    }


def test_coordinate_input_leaves_observation_context_to_client() -> None:
    method, params, include_images = _native_request(
        "click",
        screenshot_id="screenshot-7",
        x=40,
        y=60,
        button="left",
        count=1,
    )

    assert method == "click"
    assert include_images is False
    assert params == {
        "screenshot_id": "screenshot-7",
        "x": 40,
        "y": 60,
        "button": "left",
        "count": 1,
    }


def test_observe_window_selects_only_the_requested_sources() -> None:
    method, params, include_images = _native_request(
        "observe_window",
        window_id="42",
        include_screenshot=False,
        include_text=True,
    )

    assert method == "observe_window"
    assert params == {
        "window_id": "42",
        "include_screenshot": False,
        "include_text": True,
    }
    assert include_images is False


def test_observe_window_requires_one_source() -> None:
    with pytest.raises(ValueError):
        _native_request(
            "observe_window",
            window_id="42",
            include_screenshot=False,
            include_text=False,
        )


def test_coordinate_input_requires_a_current_screenshot() -> None:
    with pytest.raises(ValueError):
        _native_request("click", x=40, y=60, button="left", count=1)


@pytest.mark.parametrize(
    ("action", "values"),
    [
        ("click", {"screenshot_id": "screenshot-1", "y": 20}),
        (
            "scroll",
            {"screenshot_id": "screenshot-1", "x": 10, "delta_y": 1},
        ),
        (
            "drag",
            {
                "screenshot_id": "screenshot-1",
                "start_x": 10,
                "start_y": 20,
                "end_x": 30,
            },
        ),
    ],
)
def test_coordinate_actions_reject_missing_integers(
    action: str,
    values: dict[str, Any],
) -> None:
    with pytest.raises(ValueError):
        _native_request(action, **values)


def test_mixed_element_and_coordinate_targets_are_rejected() -> None:
    with pytest.raises(ValueError):
        _native_request(
            "click",
            element_id="uia-1",
            screenshot_id="screenshot-1",
            x=10,
            y=20,
        )
    with pytest.raises(ValueError):
        _native_request(
            "drag",
            source_element_id="uia-1",
            target_element_id="uia-2",
            screenshot_id="screenshot-1",
            start_x=10,
            start_y=20,
            end_x=30,
            end_y=40,
        )


@pytest.mark.parametrize(
    ("action", "values"),
    [
        ("double_click", {"button": "right"}),
        ("double_click", {"count": 1}),
        ("right_click", {"button": "left"}),
        ("right_click", {"count": 2}),
    ],
)
def test_fixed_click_aliases_reject_conflicting_overrides(
    action: str,
    values: dict[str, Any],
) -> None:
    with pytest.raises(ValueError):
        _native_request(action, element_id="uia-1", **values)


@pytest.mark.parametrize("delta_y", [-1200, -1, 1, 1200])
def test_scroll_uses_a_bounded_positive_down_delta(delta_y: int) -> None:
    method, params, _ = _native_request(
        "scroll",
        screenshot_id="screenshot-1",
        x=10,
        y=20,
        delta_y=delta_y,
    )

    assert method == "scroll"
    assert params["delta_y"] == delta_y


@pytest.mark.parametrize("delta_y", [-1201, 0, 1201, True, None])
def test_scroll_rejects_values_outside_its_contract(delta_y: Any) -> None:
    with pytest.raises(ValueError):
        _native_request(
            "scroll",
            screenshot_id="screenshot-1",
            x=10,
            y=20,
            delta_y=delta_y,
        )


def test_close_window_maps_to_the_native_method() -> None:
    """Closing acts through the observation and returns no screenshot."""
    method, params, include_images = _native_request("close_window")

    assert method == "close_window"
    assert params == {}
    assert include_images is False


def test_sequence_maps_a_bounded_keyboard_batch() -> None:
    steps = [
        {"action": "type", "text": "INV-001"},
        {"action": "press_key", "key": "TAB"},
    ]

    method, params, include_images = _native_request("sequence", steps=steps)

    assert method == "sequence"
    assert params == {"steps": steps}
    assert include_images is False


def test_sequence_accepts_a_json_encoded_batch() -> None:
    steps = json.dumps(
        [
            {"action": "type", "text": "INV-001"},
            {"action": "press_key", "key": "TAB"},
        ],
    )

    method, params, include_images = _native_request("sequence", steps=steps)

    assert method == "sequence"
    assert params["steps"][0]["text"] == "INV-001"
    assert include_images is False


def test_sequence_tool_schema_accepts_array_or_json_string() -> None:
    import jsonschema

    schema = FunctionTool(computer_use).input_schema
    steps = [{"action": "type", "text": "INV-001"}]

    jsonschema.validate({"action": "sequence", "steps": steps}, schema)
    jsonschema.validate(
        {"action": "sequence", "steps": json.dumps(steps)},
        schema,
    )


@pytest.mark.parametrize(
    (
        "platform",
        "buttons",
        "has_begin_text_edit",
        "key_marker",
        "scroll_marker",
    ),
    [
        ("win32", ["left", "right", "middle"], False, "Windows", "wheel"),
        ("darwin", ["left", "right"], True, "macOS", "pixel"),
    ],
)
def test_tool_schema_exposes_only_the_selected_platform_contract(
    monkeypatch: pytest.MonkeyPatch,
    platform: str,
    buttons: list[str],
    has_begin_text_edit: bool,
    key_marker: str,
    scroll_marker: str,
) -> None:
    with monkeypatch.context() as platform_patch:
        platform_patch.setattr(dispatch_module.sys, "platform", platform)
        selected = importlib.reload(dispatch_module)
        properties = FunctionTool(selected.computer_use).input_schema[
            "properties"
        ]
        actions = properties["action"]["enum"]

        assert ("begin_text_edit" in actions) is has_begin_text_edit
        assert properties["button"]["anyOf"][0]["enum"] == buttons
        assert key_marker in properties["key"]["description"]
        assert scroll_marker in properties["delta_y"]["description"]
        if platform == "win32":
            with pytest.raises(ValueError):
                selected._native_request(
                    "begin_text_edit",
                    element_id="uia-1",
                )
        else:
            method, params, _ = selected._native_request(
                "begin_text_edit",
                element_id="ax-1",
            )
            assert method == "invoke_element"
            assert params == {
                "element_id": "ax-1",
                "expects_text_input": True,
            }
    importlib.reload(dispatch_module)


def test_platform_key_normalizers_match_native_boundaries() -> None:
    assert normalize_windows_key("control+f24") == "CTRL+F24"
    assert normalize_windows_key("contextmenu") == "APPS"
    assert normalize_macos_key("win+alt+n") == "CMD+OPTION+N"
    assert normalize_macos_key("cmd+option+f20") == "CMD+OPTION+F20"
    for normalize, key in [
        (normalize_windows_key, "F25"),
        (normalize_macos_key, "F21"),
        (normalize_macos_key, "A+B"),
    ]:
        with pytest.raises(ValueError):
            normalize(key)


def test_sequence_uses_the_platform_key_contract() -> None:
    with pytest.raises(ValueError):
        _native_request(
            "sequence",
            steps=[{"action": "press_key", "key": "F25"}],
        )


@pytest.mark.parametrize(
    "steps",
    [
        [],
        "not-json",
        [{"action": "click", "x": 1, "y": 1}],
        [{"action": "type", "text": "x", "extra": True}],
        [{"action": "press_key", "key": " "}],
        [{"action": "type", "text": "x"}] * 21,
        [{"action": "type", "text": "x" * 513}],
    ],
)
def test_sequence_rejects_inputs_outside_its_contract(steps: Any) -> None:
    with pytest.raises(ValueError):
        _native_request("sequence", steps=steps)


def test_client_injects_its_private_observation() -> None:
    client = ComputerUseClient("session-1")
    client._observation_id = "observation-1"

    params = client._native_params("close_window", {})

    assert params == {"observation_id": "observation-1"}


def test_partial_sequence_result_advances_the_private_observation() -> None:
    client = ComputerUseClient("session-1")
    client._observation_id = "observation-1"

    result = client._accept_result(
        "sequence",
        {
            "observation_id": "observation-2",
            "completed_steps": 1,
            "error": {"code": "input_failed"},
        },
    )

    assert client._observation_id == "observation-2"
    assert "observation_id" not in result
    assert result["completed_steps"] == 1


def test_client_rejects_action_before_observe() -> None:
    client = ComputerUseClient("session-1")

    with pytest.raises(ComputerUseProtocolError) as refusal:
        client._native_params("click", {"x": 40, "y": 60})

    assert refusal.value.code == "observation_required"


def test_screenshot_data_stays_out_of_the_text_block() -> None:
    """Inline screenshot data must not be duplicated into the JSON text."""
    data_url = "data:image/jpeg;base64," + "A" * 4096
    payload = {
        "ok": True,
        "screenshots": [
            {
                "id": "screenshot-1",
                "url": data_url,
                "width": 800,
                "height": 600,
            },
        ],
    }

    response = _response(payload, include_images=True)

    image_blocks = [
        block for block in response.content if block.type == "data"
    ]
    text_blocks = [block for block in response.content if block.type == "text"]
    assert len(image_blocks) == 1
    assert str(image_blocks[0].source.url) == data_url
    assert len(text_blocks) == 1
    assert data_url not in text_blocks[0].text
    assert "screenshot-1" in text_blocks[0].text


def test_post_action_screenshot_metadata_survives_without_image_data() -> None:
    data_url = "data:image/jpeg;base64," + "A" * 4096
    response = _response(
        {
            "screenshots": [
                {
                    "id": "screenshot-2",
                    "kind": "transient",
                    "z_index": 1,
                    "url": data_url,
                },
            ],
        },
    )

    assert all(block.type != "data" for block in response.content)
    assert "screenshot-2" in response.content[-1].text
    assert "transient" in response.content[-1].text
    assert data_url not in response.content[-1].text


def test_native_error_marks_the_tool_call_as_failed() -> None:
    response = _error("stale_observation", "Observe the window again.")

    assert isinstance(response, ToolChunk)
    assert response.state == ToolResultState.ERROR
    assert '"ok":false' in response.content[-1].text
    assert '"requires_observe":true' in response.content[-1].text
    assert '"next_action":"observe_window"' in response.content[-1].text


def test_sequence_steps_count_against_the_action_rate_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(dispatch_module, "_action_times", [])
    monkeypatch.setattr(dispatch_module, "_MAX_ACTIONS_PER_MINUTE", 3)

    dispatch_module._check_rate_limit(3)

    with pytest.raises(ComputerUseProtocolError) as refusal:
        dispatch_module._check_rate_limit()
    assert refusal.value.code == "rate_limited"


@pytest.mark.asyncio
async def test_partial_sequence_is_an_error_with_a_fresh_observation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Enabled:
        @staticmethod
        def is_enabled() -> bool:
            return True

    class _PartialClient:
        @staticmethod
        async def execute(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
            return {
                "completed_steps": 1,
                "error": {"code": "input_failed", "step_index": 1},
                "screenshots": [],
            }

    monkeypatch.setattr(dispatch_module, "_check_rate_limit", lambda *_: None)
    monkeypatch.setattr(
        dispatch_module,
        "get_computer_use_feature_state",
        lambda: _Enabled(),
    )
    monkeypatch.setattr(
        dispatch_module,
        "get_computer_use_client",
        lambda: _PartialClient(),
    )

    response = await computer_use(
        action="sequence",
        steps=[
            {"action": "type", "text": "A"},
            {"action": "press_key", "key": "TAB"},
        ],
    )

    assert response.state == ToolResultState.ERROR
    assert '"ok":false' in response.content[-1].text
    assert '"completed_steps":1' in response.content[-1].text


@pytest.mark.asyncio
async def test_function_tool_preserves_intervention_error_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The AgentScope boundary must not turn protocol errors into success."""

    class _Enabled:
        @staticmethod
        def is_enabled() -> bool:
            return True

    class _IntervenedClient:
        @staticmethod
        async def execute(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
            raise ComputerUseProtocolError(
                "user_intervention",
                "Recent user input was detected; observe again.",
            )

    monkeypatch.setattr(dispatch_module, "_check_rate_limit", lambda: None)
    monkeypatch.setattr(
        dispatch_module,
        "get_computer_use_feature_state",
        lambda: _Enabled(),
    )
    monkeypatch.setattr(
        dispatch_module,
        "get_computer_use_client",
        lambda: _IntervenedClient(),
    )

    response = await FunctionTool(computer_use)(action="list_apps")

    assert isinstance(response, ToolChunk)
    assert response.is_last is True
    assert response.state == ToolResultState.ERROR
    assert '"code":"user_intervention"' in response.content[-1].text
    assert '"requires_observe":true' in response.content[-1].text


def test_semantic_actions_leave_observation_to_client() -> None:
    method, params, _ = _native_request(
        "invoke",
        element_id="uia-7",
    )

    assert method == "invoke_element"
    assert params == {"element_id": "uia-7"}


class _FakeTransport(ComputerUseTransport):
    def __init__(self) -> None:
        self.messages: list[dict[str, Any]] = []
        self.handler: ReverseRequestHandler | None = None
        self.closed = False

    async def connect(self) -> None:
        return None

    async def request(self, message: Mapping[str, Any]) -> dict[str, Any]:
        payload = dict(message)
        self.messages.append(payload)
        if payload["method"] == "hello":
            protocol_version = client_module.PROTOCOL_VERSION
            return {
                "request_id": payload["request_id"],
                "ok": True,
                "result": {
                    "protocol_version": protocol_version,
                },
            }
        result = {}
        if payload["method"] in client_module._OBSERVED_METHODS:
            result["observation_id"] = "observation-next"
        return {
            "request_id": payload["request_id"],
            "ok": True,
            "result": result,
        }

    async def close(self) -> None:
        self.closed = True

    def set_reverse_request_handler(
        self,
        handler: ReverseRequestHandler,
    ) -> None:
        self.handler = handler


@pytest.mark.parametrize(
    ("method", "code", "next_action"),
    [
        ("click", "target_not_at_point", "observe_window"),
        ("click", "stale_window", "list_windows"),
        ("launch_app", "user_intervention", "list_windows"),
    ],
)
@pytest.mark.asyncio
async def test_failed_action_returns_its_observation_recovery(
    method: str,
    code: str,
    next_action: str,
) -> None:
    class _ActionFailureTransport(_FakeTransport):
        async def request(self, message: Mapping[str, Any]) -> dict[str, Any]:
            if message["method"] == "hello":
                return await super().request(message)
            return {
                "request_id": message["request_id"],
                "ok": False,
                "error": {
                    "code": code,
                    "message": "Observe again.",
                },
            }

    client = ComputerUseClient("session-1", lambda: _ActionFailureTransport())
    client._observation_id = "observation-1"
    set_current_computer_use_turn_id("turn-1")
    try:
        with pytest.raises(ComputerUseProtocolError) as failure:
            await client.execute(method, {})
    finally:
        set_current_computer_use_turn_id(None)

    assert client._observation_id is None
    assert failure.value.requires_observe is True
    assert failure.value.next_action == next_action


@pytest.mark.asyncio
async def test_client_binds_session_and_turn_to_native_request() -> None:
    transport = _FakeTransport()
    client = ComputerUseClient("session-1", lambda: transport)
    set_current_computer_use_turn_id("turn-1")
    try:
        await client.execute("list_windows", {})
    finally:
        set_current_computer_use_turn_id(None)

    request = transport.messages[-1]
    assert request["method"] == "list_windows"
    assert request["meta"] == {
        "session_id": "session-1",
        "turn_id": "turn-1",
        "deadline_ms": 10000,
    }


@pytest.mark.asyncio
async def test_acquire_capability_retries_cold_start_misses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A transient acquire miss must be retried before giving up."""
    attempts: list[int] = []
    capability = runtime_module.RuntimeCapability(
        "pipe-1",
        "secret-1",
        client_module.PROTOCOL_VERSION,
    )

    def _flaky_acquire():
        attempts.append(len(attempts))
        return None if len(attempts) < 3 else capability

    monkeypatch.setattr(
        client_module.HostRuntimeProvider,
        "acquire_capability",
        _flaky_acquire,
    )
    monkeypatch.setattr(client_module, "_ACQUIRE_RETRY_DELAY_SECONDS", 0.0)

    acquired = await ComputerUseClient._acquire_capability()

    assert acquired == capability
    assert len(attempts) == 3


@pytest.mark.asyncio
async def test_acquire_capability_rejects_an_incompatible_desktop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        client_module.HostRuntimeProvider,
        "acquire_capability",
        lambda: runtime_module.RuntimeCapability(
            "pipe-1",
            "secret-1",
            client_module.PROTOCOL_VERSION + 1,
        ),
    )

    with pytest.raises(ComputerUseProtocolError) as refusal:
        await ComputerUseClient._acquire_capability()

    assert refusal.value.code == "protocol_mismatch"


@pytest.mark.asyncio
async def test_acquire_capability_gives_up_after_bounded_attempts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Persistent failures must surface instead of retrying forever."""
    attempts: list[int] = []

    def _never_acquire():
        attempts.append(len(attempts))
        return None

    monkeypatch.setattr(
        client_module.HostRuntimeProvider,
        "acquire_capability",
        _never_acquire,
    )
    monkeypatch.setattr(client_module, "_ACQUIRE_RETRY_DELAY_SECONDS", 0.0)

    acquired = await ComputerUseClient._acquire_capability()

    assert acquired is None
    assert len(attempts) == client_module._ACQUIRE_ATTEMPTS


@pytest.mark.asyncio
async def test_no_action_ever_carries_a_post_approval_exemption() -> None:
    """The client never sends after_approval, on any path.

    The exemption is gone: the recency guard has no bypass, so an action right
    after an approval is refused as retryable user_intervention rather than
    waved through by a client-held flag. The client therefore has no mechanism
    left to attach the flag, and this pins that it is absent.
    """
    transport = _FakeTransport()
    client = ComputerUseClient("session-a", lambda: transport)
    client._observation_id = "observation-1"
    set_current_computer_use_turn_id("turn-1")
    try:
        await client.execute(
            "type_text",
            {"text": "x"},
        )
        assert "after_approval" not in transport.messages[-1]["params"]

        await client.execute("click", {})
        assert "after_approval" not in transport.messages[-1]["params"]
    finally:
        set_current_computer_use_turn_id(None)


def test_the_approval_coordinator_holds_no_exemption_state() -> None:
    """Nothing to arm, so nothing to leak across turns or apps."""
    assert not hasattr(
        client_module.ComputerUseApprovalCoordinator(),
        "intervention_bypass_pending",
    )


def test_element_line_omits_windows_screen_bounds() -> None:
    """Desktop UIA bounds do not map to screenshot coordinates."""
    line = _element_line(
        {
            "id": "uia-1",
            "control_type_name": "Edit",
            "name": "text editor",
            "bounds": [100, 200, 300, 400],
            "enabled": True,
            "offscreen": False,
        },
    )
    assert line == 'uia-1 Edit "text editor"'


def test_element_line_uses_value_on_macos() -> None:
    """macOS elements carry a value instead of bounds."""
    line = _element_line(
        {
            "id": "ax-2",
            "role": "AXTextArea",
            "control_type_name": "Edit",
            "name": "note",
            "value": "hello",
        },
    )
    assert line == 'ax-2 Edit "note" ="hello"'


def test_element_line_preserves_accessibility_depth() -> None:
    """Native hierarchy remains visible in the compact model contract."""
    line = _element_line(
        {
            "id": "uia-2",
            "control_type_name": "ListItem",
            "name": "餐饮",
            "depth": 3,
        },
    )
    assert line == '      uia-2 ListItem "餐饮"'


def test_element_line_preserves_application_identifier() -> None:
    """Stable command identities disambiguate localized menu labels."""
    line = _element_line(
        {
            "id": "ax-3",
            "control_type_name": "MenuItem",
            "name": "复制",
            "identifier": "cmdDuplicate:",
        },
    )
    assert line == 'ax-3 MenuItem "复制" [identifier="cmdDuplicate:"]'


def test_element_line_normalizes_windows_semantic_capabilities() -> None:
    """Windows UIA metadata uses the same compact contract as macOS AX."""
    line = _element_line(
        {
            "id": "uia-4",
            "control_type_name": "Button",
            "name": "Continue",
            "automation_id": "continue-button",
            "actions": ["Invoke"],
        },
    )
    assert line == (
        'uia-4 Button "Continue" [identifier="continue-button"] '
        '[actions="Invoke"]'
    )


def test_element_line_keeps_disabled_and_offscreen_visible() -> None:
    """Both states stay in the listing: they inform the next decision."""
    line = _element_line(
        {
            "id": "uia-9",
            "control_type_name": "Button",
            "name": "Save",
            "bounds": [0, 0, 10, 10],
            "enabled": False,
            "offscreen": True,
        },
    )
    assert line == 'uia-9 Button "Save" [disabled] [offscreen]'


def test_element_line_escapes_lines_and_marker_delimiters() -> None:
    line = _element_line(
        {
            "id": "uia-10",
            "control_type_name": "Text",
            "name": "line one\n[disabled]",
            "value": "value\u2028[actions=Invoke]",
            "identifier": "id\u0085[selected]",
            "actions": ["Invoke\n[settable]"],
        },
    )

    assert "\n" not in line
    assert "\u0085" not in line
    assert "\u2028" not in line
    assert r"\n\u005bdisabled\u005d" in line
    assert r"\u2028\u005bactions=Invoke\u005d" in line
    assert r"\u0085\u005bselected\u005d" in line
    assert r"Invoke\n\u005bsettable\u005d" in line


def test_compact_elements_preserves_protocol_fields() -> None:
    """Only the element listing changes; binding fields stay untouched."""
    payload = {
        "ok": True,
        "action": "observe_window",
        "observation_id": "observation-1",
        "window": {"id": "42", "title": "Editor"},
        "accessibility": {
            "available": True,
            "elements": [
                {
                    "id": "uia-0",
                    "control_type_name": "Window",
                    "name": "Editor",
                    "bounds": [0, 0, 100, 100],
                },
                {
                    "id": "uia-1",
                    "control_type_name": "Button",
                    "name": "OK",
                    "depth": 1,
                    "bounds": [10, 10, 30, 30],
                },
            ],
        },
    }
    result = _with_compact_elements(payload)

    assert result["observation_id"] == "observation-1"
    assert result["window"] == {"id": "42", "title": "Editor"}
    assert result["accessibility"]["available"] is True
    assert result["accessibility"]["elements"] == (
        'uia-0 Window "Editor"\n  uia-1 Button "OK"'
    )
    # The original payload must not be mutated.
    accessibility = payload["accessibility"]
    assert isinstance(accessibility, Mapping)
    assert isinstance(accessibility["elements"], list)


def test_compact_elements_ignores_payloads_without_accessibility() -> None:
    """Input actions return no accessibility block and pass through."""
    payload = {"ok": True, "action": "click", "applied": True}
    assert _with_compact_elements(payload) == payload


def test_response_text_is_compact_and_carries_summary_fields() -> None:
    """The model-facing text drops indentation and keeps summary fields."""
    payload = {
        "ok": True,
        "action": "observe_window",
        "accessibility": {
            "available": True,
            "focused_element": 'uia-1 Edit "text editor"',
            "document_text": "hello world",
            "elements": [
                {
                    "id": "uia-1",
                    "control_type_name": "Edit",
                    "name": "text editor",
                    "bounds": [100, 200, 300, 400],
                },
            ],
        },
    }
    text = _response(payload).content[-1].text

    assert "\n  " not in text
    decoded = json.loads(text)
    accessibility = decoded["accessibility"]
    assert accessibility["focused_element"] == 'uia-1 Edit "text editor"'
    assert accessibility["document_text"] == "hello world"
    assert accessibility["elements"] == 'uia-1 Edit "text editor"'
