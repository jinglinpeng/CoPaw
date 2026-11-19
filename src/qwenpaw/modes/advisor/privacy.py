# -*- coding: utf-8 -*-
"""Construct advisor requests from approved sources and bounded choices."""

from __future__ import annotations

import copy
import json
from dataclasses import dataclass
from uuid import uuid4

from ...config.config import AdvisorPrivacyConfig
from .prompts import (
    ADVISOR_SYSTEM_PROMPT,
    CONSULT_REQUEST_TEMPLATE,
    FOLLOWUP_REQUEST_TEMPLATE,
    PLAN_REQUEST_TEMPLATE,
)
from .tools import CONSULT_TOOL_NAME, CONSULT_TOOL_DESCRIPTION

_TOPICS = {
    "interpret_requirement": "Interpret the task requirement and its conditions.",
    "select_field": "Compare the candidate fields and give the basis for choosing a field.",
    "check_relationship": "Check the relationship, join keys, and possible duplicate matches.",
    "check_aggregation": "Check the grain, filters, and aggregation rules.",
    "validate_method": "Propose checks the worker can use to validate its method.",
    "recover_execution": "Propose a recovery path using the available tools.",
}
_MAX_REFERENCES = 3
_PRIVATE_HISTORY = "Private worker history is held by the worker."
_FALLBACK_QUESTION = (
    "Review the plan's key assumptions and give checks the worker can run privately."
)


@dataclass(frozen=True)
class ApprovedAdvisorRequest:
    """A request built exclusively from approved sources."""

    text: str
    topic: str | None = None
    references: tuple[str, ...] = ()
    result_ref: str | None = None


class AdvisorPrivacy:
    """Build the advisor's view independently of the worker's history."""

    def __init__(
        self,
        config: AdvisorPrivacyConfig,
        local_results: dict[str, tuple[str, str]] | None = None,
    ) -> None:
        self.config = config.model_copy(deep=True)
        self._references = {ref: ref for ref in self.config.public_references}
        self._result_sources = frozenset(self.config.result_reference_sources)
        self._local_results = local_results if local_results is not None else {}

    def register_result(self, source: object) -> str | None:
        """Assign an opaque name to an approved tool's result."""
        if not isinstance(source, tuple) or len(source) != 2:
            return None
        capability, storage_key = source
        if (
            not isinstance(capability, str)
            or capability not in self._result_sources
            or not isinstance(storage_key, str)
            or not storage_key
        ):
            return None
        binding = (capability, storage_key)
        # A driver restart can reuse a local storage key; retire its old alias.
        for old, value in tuple(self._local_results.items()):
            if value == binding:
                del self._local_results[old]
        reference = f"R_{uuid4().hex}"
        self._local_results[reference] = binding
        return reference

    def system_prompt(self) -> str:
        return (
            ADVISOR_SYSTEM_PROMPT
            + "\n\n# Approved worker instructions and reference material\n\n"
            + self.config.public_context
        )

    def worker_tools(self, tools: list[dict]) -> list[dict]:
        """Advertise the structured format only for the protected worker."""
        updated = []
        for tool in tools:
            if tool.get("function", {}).get("name") != CONSULT_TOOL_NAME:
                updated.append(tool)
                continue
            tool = copy.deepcopy(tool)
            function = tool["function"]
            function["description"] = (
                CONSULT_TOOL_DESCRIPTION.split("Focus on one decision:", 1)[0]
                + "Use the structured question format in the argument description. "
                "Use catalog references to identify the specific decision."
            )
            function["parameters"]["properties"]["question"]["description"] = (
                'A JSON string such as {"topic":"select_field","references":[]}. '
                f"Topics: {', '.join(_TOPICS)}. "
                f"Choose at most {_MAX_REFERENCES} references from this approved catalog: "
                f"{', '.join(sorted(self._references)) or '(empty)'}. "
                "Omit all query results, observations, and free text."
            )
            if self._result_sources:
                function["parameters"]["properties"]["question"]["description"] += (
                    " To ask about one query result, add result_ref with the "
                    "exact advisor result reference from the tool response."
                )
            updated.append(tool)
        return updated

    def plan(self) -> ApprovedAdvisorRequest:
        return ApprovedAdvisorRequest(
            PLAN_REQUEST_TEMPLATE.format(
                tool_list="Use the tool instructions in the approved worker material.",
                env_section="See the approved worker material.",
                instruction=self.config.public_task,
            ),
        )

    def consultation(
        self,
        question: str,
        plan: str,
        index: int,
        limit: int,
    ) -> ApprovedAdvisorRequest:
        topic, references, result_ref = self._parse_question(question)
        rendered = _TOPICS[topic] if topic else _FALLBACK_QUESTION
        if references:
            rendered += "\nApproved references: " + ", ".join(references)
        if result_ref:
            rendered += (
                f"\nResult reference: {result_ref}. "
                "Advise how the worker should use this result locally. The "
                "worker can inspect it and apply your conditional advice."
            )
        return ApprovedAdvisorRequest(
            CONSULT_REQUEST_TEMPLATE.format(
                task=self.config.public_task,
                index=index,
                max_consults=limit,
                question=rendered,
                plan=plan,
                recent_messages=_PRIVATE_HISTORY,
            ),
            topic=topic,
            references=references,
            result_ref=result_ref,
        )

    def followup(self, plan: str, index: int, limit: int) -> ApprovedAdvisorRequest:
        return ApprovedAdvisorRequest(
            FOLLOWUP_REQUEST_TEMPLATE.format(
                task=self.config.public_task,
                index=index,
                max_interventions=limit,
                plan=plan,
                recent_messages=_PRIVATE_HISTORY,
                trigger_note="Review the plan and suggest checks the worker can apply privately.",
                severity_note="",
            ),
        )

    def _parse_question(
        self, question: str,
    ) -> tuple[str | None, tuple[str, ...], str | None]:
        """Resolve bounded choices to canonical, already-approved text."""
        if len(question) > 2048:
            return None, (), None
        try:
            data = json.loads(question)
        except (ValueError, RecursionError):
            return None, (), None
        if not isinstance(data, dict) or data.keys() - {
            "topic", "references", "result_ref",
        }:
            return None, (), None
        topic = data.get("topic")
        references = data.get("references", [])
        result_ref = data.get("result_ref")
        if (
            not isinstance(topic, str)
            or topic not in _TOPICS
            or not isinstance(references, list)
            or len(references) > _MAX_REFERENCES
            or any(
                not isinstance(ref, str) or ref not in self._references
                for ref in references
            )
            or (
                "result_ref" in data
                and (
                    not isinstance(result_ref, str)
                    or result_ref not in self._local_results
                )
            )
        ):
            return None, (), None
        return (
            topic,
            tuple(sorted({self._references[ref] for ref in references})),
            result_ref,
        )
