# -*- coding: utf-8 -*-
# pylint: disable=protected-access,unused-argument,redefined-outer-name
"""Advisor Mode privacy: tool data stays on the worker's side.

The gate is tested on its own (provenance, masking rules, restoring,
result descriptions) and end to end through ``AdvisorMiddleware`` with
canary values that must never reach the advisor.
"""

from __future__ import annotations

import json
import re

from agentscope.message import UserMsg

from qwenpaw.config.config import (
    AdvisorInterventionConfig,
    AdvisorPrivacyConfig,
)
from qwenpaw.constant import QWENPAW_MESSAGE_TAG_KEY, SCROLL_MEMORY_MESSAGE_TAG
from qwenpaw.modes.advisor.middleware import AdvisorMiddleware
from qwenpaw.modes.advisor.privacy import PLACEHOLDER_RE, AdvisorPrivacy
from qwenpaw.modes.advisor.prompts import (
    PRIVACY_ADVISOR_NOTE,
    PRIVACY_WORKER_NOTE,
)
from qwenpaw.modes.advisor.trigger import InterventionTrigger

TASK = (
    "Identify the agent with the top sales in the past five months. "
    "Return only the Id of the agent. Today's date: 2022-11-25.\n"
    "Tables: Order (Id, OwnerId, EffectiveDate), "
    "OrderItem (OrderId, Quantity, UnitPrice)"
)
SECRET_ID = "005Wt000003NIXCIA4"
SECRET_NAME = "Zanele Mofokeng"
SECRET_STATE = "Pennsylvania"
SECRET_ARTICLE = "Mandatory bundles for CollabDesign Studio quotes"
SALES_RESULT = (
    "The tool query_db was executed successfully.\n\n"
    "The result is stored under key:\nvar_dab_000004\n\n"
    "The result is:\n"
    + json.dumps(
        [
            {
                "OwnerId": SECRET_ID,
                "agent_name": SECRET_NAME,
                "sales": "46919.15",
                "ticker": "HDB",
            },
            {
                "OwnerId": "005Wt000003NIaRIAW",
                "agent_name": "Lina Park",
                "sales": "24219.03",
                "ticker": "PFE",
            },
        ],
    )
)
ERROR_RESULT = (
    "The tool query_db execution failed. The error message is:\n"
    'Conversion Error: Could not convert string "Pennsylvania" to INT32'
)


def _privacy(**overrides) -> AdvisorPrivacy:
    return AdvisorPrivacy(AdvisorPrivacyConfig(enabled=True, **overrides))


def _seen(privacy: AdvisorPrivacy) -> AdvisorPrivacy:
    """A gate that has read the task, some worker code and two results."""
    privacy.observe_public(TASK, numbers=True, declassify=True)
    privacy.observe_worker(
        "SELECT OwnerId, name AS agent_name, SUM(Quantity*UnitPrice) AS "
        "sales FROM Order JOIN OrderItem ... LIMIT 5",
    )
    privacy.observe_result(SALES_RESULT)
    privacy.observe_result(ERROR_RESULT, failed=True)
    return privacy


# ── provenance and masking ──────────────────────────────────────────────


def test_result_values_are_masked_and_restored():
    privacy = _seen(_privacy())
    text = (
        f"Order owner {SECRET_ID} ({SECRET_NAME}) leads; try {SECRET_STATE}?"
    )
    masked = privacy.mask(text)
    for secret in (SECRET_ID, SECRET_NAME, SECRET_STATE):
        assert secret not in masked
    assert PLACEHOLDER_RE.search(masked)
    assert privacy.restore(masked) == text


def test_public_text_and_the_workers_own_words_stay_readable():
    privacy = _seen(_privacy())
    text = (
        "Grouped Order by OwnerId into agent_name and sales; "
        "today is 2022-11-25, LIMIT 5, results in var_dab_000004."
    )
    assert privacy.mask(text) == text


def test_same_value_keeps_its_placeholder_across_texts():
    privacy = _seen(_privacy())
    first = privacy.mask(f"top: {SECRET_ID}")
    second = privacy.mask(f"is {SECRET_ID} right?")
    placeholder = PLACEHOLDER_RE.search(first).group(0)
    assert placeholder in second


def test_masking_twice_changes_nothing():
    privacy = _seen(_privacy())
    once = privacy.mask(f"{SECRET_NAME} has 46,919.15 in {SECRET_STATE}")
    assert privacy.mask(once) == once


def test_identifier_fragments_and_short_codes_are_masked():
    privacy = _seen(_privacy())
    masked = privacy.mask("Runner-up NIaRIAW; tickers HDB and PFE; SQL ok")
    assert "NIaRIAW" not in masked
    assert "HDB" not in masked and "PFE" not in masked
    assert "SQL" in masked


def test_an_id_tail_the_worker_matched_on_stays_data():
    """The worker filters on the tail of an id it saw in a result; writing
    that tail does not make it the worker's own word."""
    privacy = _seen(_privacy())
    privacy.observe_worker(
        "hits = [o for o in rows if 'NIaRIAW' in o['OwnerId']]",
    )
    masked = privacy.mask("Transfers: NIaRIAW->NIXCIA4; hits for o in rows")
    assert "NIaRIAW" not in masked and "NIXCIA4" not in masked
    assert "hits for o in rows" in masked


def test_number_rules():
    privacy = _seen(_privacy())
    masked = privacy.mask(
        "46,919.15 total, 0.0111d mean, 13 rows, n=1, top 5, "
        "2020-11-03 and 2021-03-24T14:30:00.000+0000, today 2022-11-25",
    )
    assert "46,919.15" not in masked and "<N_" in masked
    assert "0.0111" not in masked
    assert "13 rows" not in masked
    assert "n=1" in masked and "top 5" in masked
    assert "2020-11-03" not in masked and "2021-03-24" not in masked
    assert "14:30" not in masked
    assert "2022-11-25" in masked


def test_numbers_can_be_left_alone():
    privacy = _seen(_privacy())
    text = f"timeout 600 seconds for {SECRET_NAME}"
    masked = privacy.mask(text, numbers=False)
    assert "600" in masked and SECRET_NAME not in masked


def test_rare_text_fragments_are_masked_but_format_text_is_not():
    privacy = _privacy()
    records = [
        {"body": f"The project repo{i} is hosted on GitHub with stars"}
        for i in range(6)
    ]
    records.append({"body": SECRET_ARTICLE + " require AI DesignShift"})
    privacy.observe_result(json.dumps(records))
    masked = privacy.mask(
        "Each row reads 'is hosted on GitHub'; one says bundles for "
        "CollabDesign Studio quotes.",
    )
    assert "is hosted on GitHub" in masked
    assert "CollabDesign Studio quotes" not in masked


def test_failed_results_index_quoted_values_not_their_wording():
    privacy = _privacy()
    privacy.observe_result(ERROR_RESULT, failed=True)
    masked = privacy.mask(ERROR_RESULT)
    assert SECRET_STATE not in masked
    assert "Conversion Error: Could not convert string" in masked


def test_cut_off_previews_are_indexed_too():
    privacy = _privacy()
    preview = (
        "The result is too large, so it is stored in a file.\n"
        "The preview (first 10000 characters) of the result is:\n"
        '[{"Name": "@dummmy/pack-cli>1.0.8>lodash", "Stars": "94931"}, '
        '{"Name": "@dylanvann/sve'
    )
    privacy.observe_result(preview)
    assert "@dummmy/pack-cli>1.0.8>lodash" not in privacy.mask(
        "Name = '@dummmy/pack-cli>1.0.8>lodash'",
    )


def test_field_name_keys_are_schema_but_capitalised_keys_are_data():
    privacy = _privacy()
    privacy.observe_result(
        json.dumps({"application_number": "x1", "Sports": 14, "PA": 3}),
    )
    masked = privacy.mask("Parsed application_number; Sports and PA counts")
    assert "application_number" in masked
    assert "Sports" not in masked and " PA " not in masked


def test_record_ids_used_as_keys_are_data():
    privacy = _privacy()
    privacy.observe_result(
        json.dumps(
            {
                "ka0Wt000000Eo6cIAC": ["30 days"],
                "rowCount": 2,
                "address_line2": "x",
            },
        ),
    )
    masked = privacy.mask(
        "Article ka0Wt000000Eo6cIAC; rowCount and address_line2 parsed",
    )
    assert "ka0Wt000000Eo6cIAC" not in masked
    assert "rowCount" in masked and "address_line2" in masked


def test_names_copied_from_long_text_stay_data():
    """The worker finds a name inside a description, queries it, and the
    name then comes back as a whole cell: it never became public."""
    privacy = _privacy()
    privacy.observe_result(
        json.dumps(
            [
                {
                    "description": "Scaling the QuantumPCB Modeler is hard "
                    "for GreenStar Electronics as they grow.",
                },
            ],
        ),
    )
    privacy.observe_worker(
        "SELECT * FROM Account WHERE Name LIKE '%GreenStar Electronics%'",
    )
    privacy.observe_result(json.dumps([{"Name": "GreenStar Electronics"}]))
    masked = privacy.mask(
        "Account GreenStar Electronics runs QuantumPCB Modeler",
    )
    assert "GreenStar Electronics" not in masked
    assert "QuantumPCB Modeler" not in masked


def test_json_held_in_a_string_is_unpacked():
    privacy = _privacy()
    privacy.observe_result(
        json.dumps([{"Licenses": json.dumps(["BSD-3-Clause-Clear"])}]),
    )
    assert "BSD-3-Clause-Clear" not in privacy.mask(
        "filter Licenses for BSD-3-Clause-Clear",
    )


def test_a_row_fetched_again_is_still_one_record():
    privacy = _privacy()
    row = {"info": "The project lberrocal/npm-template is hosted on GitHub"}
    for _ in range(6):
        privacy.observe_result(json.dumps([row]), failed=False)
        privacy._observed.clear()  # each fetch is a new tool result
    masked = privacy.mask("s = 'The project lberrocal/npm-template is hosted'")
    assert "lberrocal" not in masked


def test_values_inside_chinese_text_and_after_a_hash_are_masked():
    privacy = _seen(_privacy())
    masked = privacy.mask(f"订单负责人是{SECRET_NAME}，编号#{SECRET_ID}。")
    assert SECRET_NAME not in masked and SECRET_ID not in masked
    assert "#" not in masked, "the leading # goes with the value"
    masked = privacy.mask("亚军NIaRIAW销售额24219元，代码HDB领先，日期2020-11-03起")
    for secret in ("NIaRIAW", "24219", "HDB", "2020-11-03"):
        assert secret not in masked


def test_what_the_user_writes_is_released():
    privacy = _seen(_privacy())
    assert SECRET_NAME not in privacy.mask(f"Check {SECRET_NAME}")
    privacy.observe_public(f"Now look at {SECRET_NAME} only.", declassify=True)
    assert SECRET_NAME in privacy.mask(f"Check {SECRET_NAME}")


def test_streamed_restoring_holds_back_partial_placeholders():
    privacy = _seen(_privacy())
    masked = privacy.mask(f"Return {SECRET_ID}.")
    placeholder = PLACEHOLDER_RE.search(masked).group(0)
    reply = f"Return {placeholder} now."
    shown = [
        privacy.restore_prefix(reply[:end]) for end in range(len(reply) + 1)
    ]
    for earlier, later in zip(shown, shown[1:]):
        assert later.startswith(earlier)
    assert all("<" not in part for part in shown)
    assert shown[-1] == f"Return {SECRET_ID} now."


def test_result_descriptions_never_carry_values():
    privacy = _seen(_privacy())
    desc = privacy.describe_result(SALES_RESULT)
    assert "2-10 rows" in desc
    assert "OwnerId" in desc and "agent_name" in desc
    for secret in (SECRET_ID, SECRET_NAME, "46919.15", "HDB"):
        assert secret not in desc
    preview = privacy.describe_result('The preview is:\n[{"a": "b", "c')
    assert "preview" in preview
    assert "text output" in privacy.describe_result("plain words")


# ── through the middleware ──────────────────────────────────────────────


class _State:
    def __init__(self):
        self.context = []


class _Agent:
    name = "test-agent"

    def __init__(self):
        self.state = _State()


class _EchoAdvisor:
    """Replies with ``prefix`` plus the worker's question exactly as the
    advisor received it, so restoring can be checked end to end."""

    label = "stub:advisor"

    def __init__(self, prefix=""):
        self.prefix = prefix
        self.calls: list[list[dict]] = []

    async def ask(self, messages, *, on_text=None):
        self.calls.append(messages)
        request = messages[-1]["content"]
        match = re.search(
            r"# The worker's question\n\n(.*?)\n\n# Worker's recent",
            request,
            re.S,
        )
        reply = self.prefix + (match.group(1) if match else "")
        if on_text is not None:
            for end in range(1, len(reply) + 1, 3):
                on_text(reply[:end])
            on_text(reply)
        return reply


def _assistant(blocks):
    return type(
        "Msg",
        (),
        {"role": "assistant", "name": "test-agent", "content": blocks},
    )()


def _tool_step(agent, call_id, tool, args, output):
    agent.state.context.append(
        _assistant(
            [
                {
                    "type": "tool_call",
                    "id": call_id,
                    "name": tool,
                    "input": json.dumps(args),
                },
                {
                    "type": "tool_result",
                    "id": call_id,
                    "name": tool,
                    "output": output,
                },
            ],
        ),
    )


def _middleware(advisor, privacy=True, **kw):
    mw = AdvisorMiddleware(
        advisor=advisor,
        trigger=InterventionTrigger(
            config=AdvisorInterventionConfig(consecutive_failures=2),
        ),
        privacy=_privacy() if privacy else None,
        **kw,
    )
    mw._plan_injected = True
    mw._baselined = True
    return mw


def _agent_with_data():
    agent = _Agent()
    agent.state.context.append(UserMsg(name="user", content=TASK))
    _tool_step(
        agent,
        "c1",
        "query_db",
        {
            "db_name": "products_orders",
            "query": "SELECT OwnerId, SUM(Quantity*UnitPrice) AS sales "
            "FROM Order GROUP BY 1",
        },
        SALES_RESULT,
    )
    _tool_step(
        agent,
        "c2",
        "query_db",
        {
            "db_name": "core_crm",
            "query": f"SELECT * FROM User WHERE Id = '{SECRET_ID}'",
        },
        ERROR_RESULT,
    )
    agent.state.context.append(
        _assistant(
            [{"type": "text", "text": f"{SECRET_NAME} leads with 46,919.15."}],
        ),
    )
    return agent


async def _model_call(mw, agent, system_text="You are a data agent."):
    async def handler(**kwargs):
        return kwargs

    await mw.on_model_call(
        agent,
        {
            "messages": [{"role": "system", "content": system_text}],
            "tools": [],
        },
        handler,
    )


def _sent(advisor) -> str:
    return "\n".join(m["content"] for call in advisor.calls for m in call)


async def test_consultation_keeps_tool_data_from_the_advisor():
    advisor = _EchoAdvisor(prefix="Answer: ")
    mw, agent = _middleware(advisor), _agent_with_data()
    await _model_call(mw, agent)
    question = (
        f"Order owner {SECRET_ID} ({SECRET_NAME}) has 46,919.15; the "
        f"other query failed on {SECRET_STATE}. Order or opportunity owner?"
    )
    answer = await mw.consult(question)

    sent = _sent(advisor)
    for secret in (SECRET_ID, SECRET_NAME, SECRET_STATE, "46,919.15"):
        assert secret not in sent
    assert "Lina Park" not in sent and "24219.03" not in sent
    assert "[result kept on the worker's side: 2-10 rows" in sent
    assert "Conversion Error" in sent, "the error wording is forwarded"
    assert PRIVACY_ADVISOR_NOTE in advisor.calls[0][0]["content"]
    assert answer.startswith(f"Answer: {question}")
    assert PRIVACY_WORKER_NOTE in answer
    record = mw.transcript["calls"][-1]
    assert record["privacy"]["final_pass_masked"] is False
    assert mw.consults[-1]["question"] == question


async def test_id_tails_in_the_question_stay_masked():
    """The consult call is in the context when the question is masked; its
    own arguments must not release the id tails it quotes."""
    advisor = _EchoAdvisor(prefix="")
    mw, agent = _middleware(advisor), _agent_with_data()
    await _model_call(mw, agent)
    question = "Only transfer: NIaRIAW->NIXCIA4. Return the full Id?"
    agent.state.context.append(
        _assistant(
            [
                {
                    "type": "tool_call",
                    "id": "c3",
                    "name": "consult_advisor",
                    "input": json.dumps({"question": question}),
                },
            ],
        ),
    )
    await mw.consult(question)
    sent = _sent(advisor)
    assert "NIaRIAW" not in sent and "NIXCIA4" not in sent


async def test_tool_call_arguments_are_masked_inside_their_json():
    advisor = _EchoAdvisor()
    mw, agent = _middleware(advisor), _agent_with_data()
    _tool_step(
        agent,
        "c3",
        "execute_python",
        {"code": f"rows = [\n'{SECRET_NAME}',\n46919.15]\nprint(rows)"},
        '{"n": 1}',
    )
    await _model_call(mw, agent)
    await mw.consult("Which table next?")
    sent = _sent(advisor)
    assert SECRET_NAME not in sent and "46919.15" not in sent
    assert "execute_python(" in sent


async def test_streamed_consultation_reaches_the_worker_restored():
    advisor = _EchoAdvisor()
    mw, agent = _middleware(advisor), _agent_with_data()
    await _model_call(mw, agent)
    question = f"Is {SECRET_ID} right?"
    pieces = [piece async for piece in mw.consult_stream(question)]
    joined = "".join(pieces)
    assert joined.startswith(question)
    assert not any(PLACEHOLDER_RE.search(p) for p in pieces)


async def test_followup_request_is_masked_and_the_advice_restored():
    advisor = _EchoAdvisor()
    mw, agent = _middleware(advisor), _agent_with_data()
    await _model_call(mw, agent)
    secret_value = PLACEHOLDER_RE.search(mw._privacy.mask(SECRET_STATE)).group(
        0
    )

    async def reply_adjust(messages, *, on_text=None):
        advisor.calls.append(messages)
        return f"ADJUST\nStop filtering on {secret_value}; cast first."

    advisor.ask = reply_adjust
    for i in range(2):
        _tool_step(
            agent,
            f"f{i}",
            "execute_shell_command",
            {"command": f"psql -c 'select {i}'"},
            "Command failed with exit code 1.",
        )
    injected = await mw._check_and_intervene(agent)
    assert injected is not None
    assert SECRET_STATE not in _sent(advisor)
    result = injected.content[1]
    assert f"Stop filtering on {SECRET_STATE}; cast first." in result.output


async def test_worker_system_messages_are_masked_but_keep_their_numbers():
    advisor = _EchoAdvisor()
    mw, agent = _middleware(advisor), _agent_with_data()
    await _model_call(mw, agent)
    await _model_call(
        mw,
        agent,
        system_text=f"Timeout 600s. Last answer mentioned {SECRET_NAME}.",
    )
    await mw.consult("Next?")
    system = advisor.calls[-1][0]["content"]
    assert "Timeout 600s" in system
    assert SECRET_NAME not in system


async def test_compacted_history_is_data_and_never_the_task():
    advisor = _EchoAdvisor()
    mw, agent = _middleware(advisor), _Agent()
    agent.state.context.append(UserMsg(name="user", content=TASK))
    memory = UserMsg(
        name="memory",
        content=f"<system-info>Earlier: {SECRET_NAME} topped the ranking.",
        metadata={QWENPAW_MESSAGE_TAG_KEY: SCROLL_MEMORY_MESSAGE_TAG},
    )
    agent.state.context.append(memory)
    await _model_call(mw, agent)
    assert mw._task == TASK
    await mw.consult(f"Is {SECRET_NAME} still the answer?")
    assert SECRET_NAME not in _sent(advisor)


async def test_privacy_off_forwards_results_as_before():
    advisor = _EchoAdvisor()
    mw, agent = _middleware(advisor, privacy=False), _agent_with_data()
    await _model_call(mw, agent)
    await mw.consult(f"Is {SECRET_ID} right?")
    sent = _sent(advisor)
    assert SECRET_ID in sent and SECRET_NAME in sent
    assert "result kept on the worker's side" not in sent
    assert PRIVACY_ADVISOR_NOTE not in sent
    assert "privacy" not in mw.transcript["calls"][-1]
