# -*- coding: utf-8 -*-
"""The JEV level does not ask the decision model about Computer Use.

Computer Use gates its own effects (per-app approval, forbidden windows, user
input preemption); the decision model would only see the action name. These
tests register the real tool the way the plugin does and run the real
governance check, so the exemption is proven against the actual callable.
"""

# pylint: disable=protected-access

from types import SimpleNamespace

import pytest

from qwenpaw.governance import PolicyGuardedTool
from qwenpaw.governance.policy import GovernanceAction, GovernanceDecision
from qwenpaw.governance.tool_registry import DEFAULT_REGISTRY
from qwenpaw.plugins.api import _bridge_to_runtime, _register_to_governance
from qwenpaw.providers.typesafe_provider import TypeSafeEvaluationError

from computer_use import computer_use


@pytest.fixture(name="registered_computer_use")
def _registered_computer_use(monkeypatch):
    """Register ``computer_use`` exactly as ``PluginApi.register_tool`` does."""
    # Restored afterwards: registration replaces the descriptor in place.
    monkeypatch.setattr(
        computer_use,
        "_tool_descriptor",
        computer_use._tool_descriptor,
    )
    previous_owner = DEFAULT_REGISTRY.get_owner("computer_use")
    _register_to_governance(
        "computer_use",
        tool_type="internal",
        target_param="action",
        owner="computer-use",
    )
    _bridge_to_runtime(
        "computer_use",
        computer_use,
        True,
        "",
        None,
        "computer-use",
    )
    yield computer_use
    if previous_owner is None:
        DEFAULT_REGISTRY.unregister_python_tool("computer_use")


def _guarded(func):
    governor = SimpleNamespace(
        policy=SimpleNamespace(execution_level="auto"),
        assert_policy=lambda _spec: GovernanceDecision(
            GovernanceAction.ALLOW,
            "internal",
        ),
        audit=lambda _spec, _decision: None,
        workspace_dir="",
    )
    return PolicyGuardedTool(
        func,
        governor=governor,
        request_context={"approval_level": "JEV", "session_id": "session"},
    )


@pytest.fixture(name="decision_calls")
def _decision_calls(monkeypatch):
    calls = []

    def unavailable(agent_id=""):
        calls.append(agent_id)
        raise TypeSafeEvaluationError("no decision model in tests")

    monkeypatch.setattr(
        "qwenpaw.security.decision_model._decision_provider",
        unavailable,
    )
    return calls


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["observe_window", "click", "set_value"])
async def test_jev_level_runs_computer_use_without_decision_model(
    registered_computer_use,
    decision_calls,
    action,
):
    tool = _guarded(registered_computer_use)
    result = await tool.check_permissions(
        {"action": action, "element_id": "e1"},
    )
    assert result.behavior.value == "allow"
    assert not decision_calls


@pytest.mark.asyncio
async def test_same_name_function_still_reaches_decision_model(
    monkeypatch,
    registered_computer_use,
    decision_calls,
):
    del registered_computer_use
    asked = []

    async def ask_user(*_args, **_kwargs):
        asked.append(True)
        return SimpleNamespace(behavior=SimpleNamespace(value="ask"))

    # An unavailable decision model turns the call into a user approval,
    # which would otherwise wait for a person.
    monkeypatch.setattr(
        "qwenpaw.governance.tool_adapter._ask_user_approval",
        ask_user,
    )

    async def impostor(action: str = ""):
        return action

    impostor.__name__ = "computer_use"
    tool = _guarded(impostor)
    result = await tool.check_permissions({"action": "click"})
    assert result.behavior.value == "ask"
    assert asked
    assert len(decision_calls) == 1
