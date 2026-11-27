#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Summarize the main-model calls of one QwenPaw run from the proxy log.

    analyze.py --calls runs/llm_calls.jsonl --run runs/gedit-1.run.json [--sidecar cu-state/sidecar.jsonl] [--json out.json]

A main-model call is a chat completion that offered tools to the model, i.e.
one turn of the agent loop. Side calls without tools (chat title generation)
are counted separately. Each agent call is labelled with what the model did in
it: the computer_use action it requested, another tool, or the final reply.
"""

import argparse
import json
from collections import Counter


def load_calls(path, start, end):
    calls = []
    with open(path) as handle:
        for line in handle:
            entry = json.loads(line)
            if not entry["path"].endswith("/chat/completions"):
                continue
            if start - 1 <= entry["start"] <= end + 1:
                calls.append(entry)
    return calls


def requested_tools(call, following):
    """The tool calls a model turn produced.

    The next request replays that turn as its last assistant message, which
    is the authoritative record; the streamed response is the fallback for
    the final turn.
    """
    if following is not None:
        before = (call["request"] or {}).get("messages") or []
        after = (following["request"] or {}).get("messages") or []
        if len(after) > len(before):
            # Parallel calls may be replayed as several assistant messages.
            tools = [
                {
                    "name": (c.get("function") or {}).get("name", ""),
                    "arguments": (c.get("function") or {}).get(
                        "arguments",
                        "",
                    ),
                }
                for message in after[len(before) :]
                if message.get("role") == "assistant"
                for c in message.get("tool_calls") or []
            ]
            if tools:
                return tools
    return call["response"]["tool_calls"]


def describe(call, following=None):
    actions = []
    for tool in requested_tools(call, following):
        try:
            arguments = json.loads(tool["arguments"] or "{}")
        except ValueError:
            arguments = {"raw": tool["arguments"][:200]}
        if tool["name"] == "computer_use":
            action = arguments.get("action", "?")
            detail = {
                k: arguments[k]
                for k in (
                    "window_id",
                    "element_id",
                    "key",
                    "text",
                    "value",
                    "app",
                )
                if k in arguments
            }
            if action == "sequence":
                detail["steps"] = arguments.get("steps")
            actions.append(("computer_use:" + action, detail))
        else:
            actions.append(
                (
                    tool["name"],
                    {k: str(v)[:120] for k, v in arguments.items()},
                ),
            )
    return actions


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--calls", required=True)
    parser.add_argument("--run", required=True)
    parser.add_argument("--sidecar")
    parser.add_argument("--json")
    args = parser.parse_args()
    run = json.load(open(args.run))
    calls = load_calls(args.calls, run["started"], run["finished"])
    agent = [c for c in calls if (c["request"] or {}).get("tools")]
    side = [c for c in calls if not (c["request"] or {}).get("tools")]
    rows = []
    for index, call in enumerate(agent, 1):
        usage = call["response"]["usage"] or {}
        actions = describe(call, agent[index] if index < len(agent) else None)
        rows.append(
            {
                "n": index,
                "seconds": round(call["ms"] / 1000.0, 1),
                "prompt_tokens": usage.get("prompt_tokens"),
                "completion_tokens": usage.get("completion_tokens"),
                "images_in_request": call["images"],
                "messages": len((call["request"] or {}).get("messages") or []),
                "actions": actions,
                "text": call["response"]["text"][:300],
            },
        )
    kinds = Counter()
    for row in rows:
        if not row["actions"]:
            kinds["final reply"] += 1
        for name, _detail in row["actions"]:
            kinds[name] += 1
    summary = {
        "session_id": run["session_id"],
        "wall_seconds": run["seconds"],
        "agent_calls": len(rows),
        "side_calls": len(side),
        "model_seconds": round(sum(r["seconds"] for r in rows), 1),
        "prompt_tokens": sum(r["prompt_tokens"] or 0 for r in rows),
        "completion_tokens": sum(r["completion_tokens"] or 0 for r in rows),
        "tool_calls_by_kind": dict(kinds),
        "status_errors": [c["status"] for c in calls if c["status"] >= 400],
    }
    events_path = args.run.replace(".run.json", ".events.jsonl")
    try:
        with open(events_path) as handle:
            events = [json.loads(line) for line in handle if line.strip()]
        # Thinking is on by default, so every model turn opens with exactly
        # one reasoning message: an independent count of agent calls.
        summary["stream_reasoning_messages"] = sum(
            1
            for e in events
            if e.get("type") == "reasoning" and e.get("status") == "completed"
        )
        summary["stream_turn_usage_events"] = sum(
            1 for e in events if e.get("type") == "turn_usage"
        )
    except OSError:
        pass
    if args.sidecar:
        ops, failures = Counter(), []
        with open(args.sidecar) as handle:
            for line in handle:
                entry = json.loads(line)
                if run["started"] <= entry["t"] <= run["finished"] + 1:
                    ops[entry["op"]] += 1
                    if not entry.get("ok"):
                        failures.append(entry)
        summary["platform_ops"] = dict(ops)
        summary["platform_failures"] = failures
    print(json.dumps(summary, ensure_ascii=False, indent=1))
    for row in rows:
        label = "; ".join(
            "%s %s" % (name, json.dumps(detail, ensure_ascii=False)[:160])
            for name, detail in row["actions"]
        ) or ("FINAL: " + row["text"][:120])
        print(
            "%2d  %5.1fs  in=%-6s out=%-5s img=%d  %s"
            % (
                row["n"],
                row["seconds"],
                row["prompt_tokens"],
                row["completion_tokens"],
                row["images_in_request"],
                label,
            ),
        )
    if args.json:
        with open(args.json, "w") as handle:
            json.dump(
                {"summary": summary, "calls": rows},
                handle,
                ensure_ascii=False,
                indent=1,
            )


if __name__ == "__main__":
    main()
