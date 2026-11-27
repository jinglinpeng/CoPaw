#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Talk to the Computer Use helper directly, without QwenPaw.

Speaks the helper's framed protocol (4-byte little-endian length + JSON),
authenticates with the capability in $STATE/helper.env, and approves every
application it is asked about. Used to check the Linux platform layer before
an agent drives it:

    python3 probe.py                       # list apps and windows
    python3 probe.py observe <window_id>   # print one observation
"""

import json
import os
import socket
import struct
import sys

PROTOCOL_VERSION = 2
STATE = os.environ.get(
    "CU_STATE",
    os.path.expanduser("~/.cache/qwenpaw-cu-linux"),
)


def load_capability():
    values = {}
    with open(os.path.join(STATE, "helper.env")) as handle:
        for line in handle:
            line = line.strip()
            if line.startswith("export ") and "=" in line:
                name, value = line[len("export ") :].split("=", 1)
                values[name] = value.strip("'")
    return (
        values["QWENPAW_COMPUTER_USE_PIPE"],
        values["QWENPAW_COMPUTER_USE_CAPABILITY"],
    )


class Client(object):
    def __init__(self):
        path, self.secret = load_capability()
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.connect(path)
        self.counter = 0
        self.approvals = []
        reply = self._exchange(
            {
                "request_id": "hello",
                "method": "hello",
                "params": {"capability": self.secret},
            },
        )
        if not reply.get("ok"):
            raise RuntimeError("hello failed: %s" % reply)

    def _send(self, message):
        payload = json.dumps(message).encode("utf-8")
        self.sock.sendall(struct.pack("<I", len(payload)) + payload)

    def _recv(self):
        header = self._read(4)
        (length,) = struct.unpack("<I", header)
        return json.loads(self._read(length).decode("utf-8"))

    def _read(self, size):
        data = b""
        while len(data) < size:
            chunk = self.sock.recv(size - len(data))
            if not chunk:
                raise RuntimeError("helper closed the connection")
            data += chunk
        return data

    def _exchange(self, message):
        self._send(message)
        while True:
            reply = self._recv()
            if reply.get("method") == "request_app_approval":
                self.approvals.append(
                    reply.get("params", {}).get("canonical_app_id"),
                )
                self._send(
                    {
                        "request_id": reply["request_id"],
                        "ok": True,
                        "result": {"decision": "allow"},
                    },
                )
                continue
            if reply.get("request_id") == message["request_id"]:
                return reply

    def call(self, method, **params):
        self.counter += 1
        return self._exchange(
            {
                "request_id": "probe-%d" % self.counter,
                "method": method,
                "params": params,
                "protocol_version": PROTOCOL_VERSION,
                "meta": {},
            },
        )


def summarize(observation):
    result = observation.get("result", observation)
    accessibility = result.get("accessibility", {})
    lines = [
        "observation %s window %s"
        % (result.get("observation_id"), result.get("window")),
        "viewport %s visual %s screenshots %d"
        % (
            result.get("viewport"),
            result.get("visual"),
            len(result.get("screenshots") or []),
        ),
        "focused %s" % accessibility.get("focused_element"),
    ]
    for element in accessibility.get("elements") or []:
        flags = [
            k
            for k in ("settable", "focused", "selected", "offscreen")
            if element.get(k)
        ]
        if element.get("enabled") is False:
            flags.append("disabled")
        lines.append(
            "%s%s %s %r %s %s%s"
            % (
                "  " * min(element.get("depth", 0), 12),
                element["id"],
                element["role"],
                element["name"][:50],
                ",".join(element.get("actions") or []),
                " ".join(flags),
                (" =%r" % element["value"]) if "value" in element else "",
            ),
        )
    return "\n".join(lines)


def main():
    client = Client()
    if len(sys.argv) >= 3 and sys.argv[1] == "observe":
        print(summarize(client.call("observe_window", window_id=sys.argv[2])))
        return
    print(json.dumps(client.call("list_apps"), indent=1)[:4000])


if __name__ == "__main__":
    main()
