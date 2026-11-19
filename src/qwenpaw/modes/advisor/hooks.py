# -*- coding: utf-8 -*-
"""Advisor mode hooks."""

from __future__ import annotations

import logging
from typing import Any

from agentscope.message import AssistantMsg, TextBlock

from ..base import ModeGatedHook
from ...providers.model_selection import (
    request_model_override,
    resolve_model_slot_override,
)
from ...runtime.hooks import HookAction, HookContext, HookResult
from ...runtime.phases import Phase
from .config import resolve_agent_config
from .models import (
    resolve_worker_slot,
    resolve_advisor_slot,
    slot_label,
    slot_to_dict,
)

logger = logging.getLogger(__name__)


class WorkerModelHook(ModeGatedHook):
    """Run the agent on the worker model while the advisor keeps its own.

    Sets the worker slot (see :func:`resolve_worker_slot`) as
    ``model_slot_override`` on the request before :class:`AgentBuilder`
    builds the model, the same path a spawned subagent uses. An override
    already on the request wins.
    """

    phase = Phase.PRE_AGENT_BUILD
    name = "advisor_mode_worker_model"
    priority = 30

    async def _run(self, ctx: HookContext) -> HookResult:
        cfg = resolve_agent_config(ctx)
        worker = slot_to_dict(resolve_worker_slot(cfg))
        request = getattr(ctx, "request", None)
        override = request_model_override(request)
        if cfg is not None and cfg.advisor_mode.privacy is not None:
            advisor = slot_to_dict(resolve_advisor_slot(cfg))
            if (
                request is None
                or worker is None
                or advisor is None
                or worker == advisor
                or (
                    override is not None
                    and slot_to_dict(resolve_model_slot_override(override))
                    != worker
                )
            ):
                return HookResult(
                    action=HookAction.SHORT_CIRCUIT,
                    payload=AssistantMsg(
                        name="assistant",
                        content=[TextBlock(
                            type="text",
                            text=(
                                "Advisor privacy requires the configured "
                                "worker and a distinct advisor model."
                            ),
                        )],
                    ),
                )
        if worker is None:
            logger.info(
                "Advisor Mode: no worker model configured (sub-agent "
                "model or advisor_mode.worker_model). The agent and the "
                "advisor both run on the primary model",
            )
        elif request is None or override is not None:
            logger.debug(
                "Advisor Mode: request already carries a model override, "
                "leaving it alone",
            )
        else:
            request.model_slot_override = dict(worker)
            logger.info(
                "Advisor Mode: worker runs on %s:%s (advisor: %s)",
                worker["provider_id"],
                worker["model"],
                slot_label(resolve_advisor_slot(cfg)),
            )
        return HookResult()


__all__ = ["WorkerModelHook"]
