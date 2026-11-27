#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Forwarding proxy that records every main-model request QwenPaw makes.

Point the DashScope provider's base URL at this proxy (same path, local
host). Each request is forwarded unchanged to the upstream host and streamed
back; one JSON line per request is appended to the log:

    {"seq", "start", "ms", "path", "status", "request": {...}, "response": {...}}

`request` is the JSON body with image data URLs replaced by their length, and
`response` is reassembled from the server-sent events (text, reasoning, tool
calls, usage). Authorization headers are forwarded but never written.

    record_proxy.py --port <free port> --upstream https://dashscope.aliyuncs.com --log calls.jsonl
"""

import argparse
import asyncio
import json
import re
import time

import aiohttp
from aiohttp import web

HOP_HEADERS = {
    "host",
    "content-length",
    "transfer-encoding",
    "connection",
    "keep-alive",
    "accept-encoding",
    "content-encoding",
}
DATA_URL = re.compile(r"data:image/[a-z+]+;base64,[A-Za-z0-9+/=]+")


def redact_images(value):
    """Keep the request readable: replace each embedded image by its size."""
    if isinstance(value, str):
        return DATA_URL.sub(
            lambda m: "<image %d bytes>" % len(m.group(0)),
            value,
        )
    if isinstance(value, list):
        return [redact_images(item) for item in value]
    if isinstance(value, dict):
        return {key: redact_images(item) for key, item in value.items()}
    return value


def count_images(value):
    if isinstance(value, str):
        return len(DATA_URL.findall(value))
    if isinstance(value, list):
        return sum(count_images(item) for item in value)
    if isinstance(value, dict):
        return sum(count_images(item) for item in value.values())
    return 0


def assemble(events):
    """Fold OpenAI-style streaming chunks into one response summary."""
    text, reasoning, usage, finish = [], [], None, None
    calls, ids = {}, {}
    for event in events:
        if event.get("usage"):
            usage = event["usage"]
        for choice in event.get("choices") or []:
            delta = choice.get("delta") or choice.get("message") or {}
            if delta.get("content"):
                text.append(delta["content"])
            if delta.get("reasoning_content"):
                reasoning.append(delta["reasoning_content"])
            for call in delta.get("tool_calls") or []:
                # Parallel calls may share an index; a new id starts a new call.
                if call.get("id") and call["id"] not in ids:
                    ids[call["id"]] = len(ids)
                key = ids.get(
                    call.get("id"),
                    len(ids) - 1 if ids else call.get("index", 0),
                )
                slot = calls.setdefault(key, {"name": "", "arguments": ""})
                function = call.get("function") or {}
                slot["name"] += function.get("name") or ""
                slot["arguments"] += function.get("arguments") or ""
            if choice.get("finish_reason"):
                finish = choice["finish_reason"]
    return {
        "text": "".join(text),
        "reasoning_chars": len("".join(reasoning)),
        "tool_calls": [calls[key] for key in sorted(calls)],
        "finish_reason": finish,
        "usage": usage,
    }


class Recorder(object):
    def __init__(self, upstream, log_path):
        self.upstream = upstream.rstrip("/")
        self.log_path = log_path
        self.seq = 0
        self.session = None

    async def handle(self, request):
        if self.session is None:
            self.session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=600),
            )
        self.seq += 1
        seq = self.seq
        started = time.time()
        body = await request.read()
        headers = {
            k: v
            for k, v in request.headers.items()
            if k.lower() not in HOP_HEADERS
        }
        url = self.upstream + request.rel_url.path_qs
        try:
            payload = json.loads(body) if body else None
        except ValueError:
            payload = None
        async with self.session.request(
            request.method,
            url,
            data=body,
            headers=headers,
        ) as upstream:
            response = web.StreamResponse(status=upstream.status)
            for key, value in upstream.headers.items():
                if key.lower() not in HOP_HEADERS:
                    response.headers[key] = value
            await response.prepare(request)
            raw = bytearray()
            client_gone = False
            async for chunk in upstream.content.iter_any():
                raw.extend(chunk)
                if client_gone:
                    continue
                try:
                    await response.write(chunk)
                except (ConnectionError, aiohttp.ClientConnectionResetError):
                    # The client may hang up as soon as it has the last event;
                    # keep reading so the record is still complete.
                    client_gone = True
            if not client_gone:
                try:
                    await response.write_eof()
                except (ConnectionError, aiohttp.ClientConnectionResetError):
                    client_gone = True
            status = upstream.status
        events = []
        for line in raw.decode("utf-8", "replace").splitlines():
            line = line.strip()
            if line.startswith("data:"):
                data = line[5:].strip()
                if data and data != "[DONE]":
                    try:
                        events.append(json.loads(data))
                    except ValueError:
                        pass
        if not events:
            try:
                events = [json.loads(bytes(raw))]
            except ValueError:
                events = []
        entry = {
            "seq": seq,
            "start": round(started, 3),
            "ms": int((time.time() - started) * 1000),
            "path": request.rel_url.path,
            "status": status,
            "images": count_images(payload),
            "request": redact_images(payload),
            "response": assemble(events),
        }
        if status >= 400:
            entry["error_body"] = raw.decode("utf-8", "replace")[:2000]
        with open(self.log_path, "a") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
        return response


def main():
    parser = argparse.ArgumentParser()
    # No default: a fixed port may already belong to another local service,
    # which would then receive the forwarded Authorization header.
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--upstream", default="https://dashscope.aliyuncs.com")
    parser.add_argument("--log", required=True)
    args = parser.parse_args()
    recorder = Recorder(args.upstream, args.log)
    app = web.Application(client_max_size=256 * 1024 * 1024)
    app.router.add_route("*", "/{tail:.*}", recorder.handle)
    web.run_app(app, host="127.0.0.1", port=args.port, print=None)


if __name__ == "__main__":
    main()
