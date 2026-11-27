#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Send one task to a local QwenPaw console and wait for it to finish.

Streams the console chat (the same endpoint the web UI uses), approves any
pending tool approval the way a user clicking "allow" would, and writes a run
record next to the given output prefix:

    <prefix>.events.jsonl   raw stream events
    <prefix>.run.json       session id, timing, approvals, final reply

    run_task.py --port 18790 --session cu-gedit-1 --out runs/gedit-1 "task text"
"""

import argparse
import json
import threading
import time
import urllib.error
import urllib.request


def api(base, method, path, body=None, timeout=30):
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(
        base + path,
        data=data,
        method=method,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        raw = response.read()
        return json.loads(raw) if raw else None


def approver(base, session_id, stop, approvals):
    """Approve pending tool approvals for this session, as a user would."""
    while not stop.is_set():
        try:
            listing = api(
                base,
                "GET",
                "/approval/list?session_id=%s" % session_id,
            )
        except Exception:
            listing = None
        for item in (listing or {}).get("pending_approvals") or []:
            request_id = item.get("request_id")
            if not request_id or request_id in {
                a["request_id"] for a in approvals
            }:
                continue
            try:
                api(
                    base,
                    "POST",
                    "/approval/approve",
                    {
                        "request_id": request_id,
                        "session_id": item.get("session_id") or session_id,
                    },
                )
                approvals.append(
                    {
                        "request_id": request_id,
                        "t": time.time(),
                        "summary": str(item)[:300],
                    },
                )
            except Exception as error:
                approvals.append(
                    {
                        "request_id": request_id,
                        "t": time.time(),
                        "error": str(error),
                    },
                )
        stop.wait(1.0)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("task")
    parser.add_argument("--port", type=int, default=18790)
    parser.add_argument("--session", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--approval-level", default="SMART")
    parser.add_argument("--timeout", type=int, default=1800)
    args = parser.parse_args()
    base = "http://127.0.0.1:%d/api" % args.port
    body = {
        "input": [
            {"role": "user", "content": [{"type": "text", "text": args.task}]},
        ],
        "session_id": args.session,
        "user_id": "cu-linux-test",
        "request_context": {"approval_level": args.approval_level},
    }
    approvals = []
    stop = threading.Event()
    watcher = threading.Thread(
        target=approver,
        args=(base, args.session, stop, approvals),
        daemon=True,
    )
    watcher.start()
    started = time.time()
    events = 0
    final_text = []
    request = urllib.request.Request(
        base + "/console/chat",
        data=json.dumps(body).encode(),
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
        },
    )
    with urllib.request.urlopen(
        request,
        timeout=args.timeout,
    ) as response, open(args.out + ".events.jsonl", "w") as log:
        for raw in response:
            line = raw.decode("utf-8", "replace").strip()
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if not data or data == "[DONE]":
                continue
            events += 1
            log.write(data + "\n")
            try:
                event = json.loads(data)
            except ValueError:
                continue
            if (
                event.get("object") == "message"
                and event.get("role") == "assistant"
                and event.get("status") == "completed"
                and event.get("type") == "message"
            ):
                for part in event.get("content") or []:
                    if part.get("type") == "text" and part.get("text"):
                        final_text.append(part["text"])
    finished = time.time()
    stop.set()
    record = {
        "session_id": args.session,
        "task": args.task,
        "approval_level": args.approval_level,
        "started": started,
        "finished": finished,
        "seconds": round(finished - started, 1),
        "events": events,
        "approvals": approvals,
        "final_text": final_text[-1] if final_text else "",
    }
    with open(args.out + ".run.json", "w") as handle:
        json.dump(record, handle, ensure_ascii=False, indent=1)
    print(
        json.dumps(
            {
                k: record[k]
                for k in ("session_id", "seconds", "events", "approvals")
            },
            ensure_ascii=False,
        ),
    )
    print("FINAL:", record["final_text"][:1500])


if __name__ == "__main__":
    main()
