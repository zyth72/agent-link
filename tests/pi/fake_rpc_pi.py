#!/usr/bin/env python3
"""Fake ``pi --mode rpc`` process used by tests/test_pi_rpc.py.

The real protocol is simulated from environment variables only; the controller
is exercised with strict JSONL framing, out-of-order ids, async events, bad
frames and abrupt exits.  It never calls a model and never opens a network
connection.  Arguments are ignored except for being logged (argv can be
asserted through FAKE_PI_ARGV_LOG).
"""

import json
import os
import sys
import time

LINE_SEP = "\u2028"

LOG_PATH = os.environ.get("FAKE_PI_LOG")
ARGV_PATH = os.environ.get("FAKE_PI_ARGV_LOG")
SESSION_ID = os.environ.get("FAKE_PI_SESSION_ID", "fake-pi-session")
SCENARIO = os.environ.get("FAKE_PI_SCENARIO", "normal")


def log(record):
    if not LOG_PATH:
        return
    with open(LOG_PATH, "a", encoding="utf-8") as stream:
        stream.write(json.dumps(record, ensure_ascii=False) + "\n")


def emit(record):
    sys.stdout.write(json.dumps(record, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def state_data():
    return {
        "sessionId": SESSION_ID,
        "model": {"id": "fake/model", "provider": "fake"},
        "thinkingLevel": "medium",
        "isStreaming": False,
        "isCompacting": False,
        "steeringMode": "all",
        "followUpMode": "one-at-a-time",
        "sessionName": f"fake{LINE_SEP}name",
        "messageCount": 0,
        "pendingMessageCount": 0,
    }


def respond(record, *, success=True, data=None, error=None):
    response = {
        "id": record.get("id"),
        "type": "response",
        "command": record.get("type"),
        "success": success,
    }
    if data is not None:
        response["data"] = data
    if error is not None:
        response["error"] = error
    emit(response)


def normal_data(record):
    kind = record.get("type")
    if kind == "get_state":
        return state_data()
    if kind == "get_messages":
        return {"messages": [{"role": "assistant", "content": f"fake{LINE_SEP}message"}]}
    if kind in ("prompt", "steer", "follow_up"):
        return {"disposition": "queued" if kind != "prompt" else "started"}
    if kind == "clear_queue":
        return {"steering": ["queued-steer"], "followUp": ["queued-follow"]}
    return {}


def main():
    if ARGV_PATH:
        with open(ARGV_PATH, "w", encoding="utf-8") as stream:
            json.dump(
                {
                    "argv": sys.argv,
                    "env": {
                        "AGENT_LINK_CREDENTIALS": os.environ.get("AGENT_LINK_CREDENTIALS"),
                        "AGENT_LINK_BRIDGE_FILE": os.environ.get("AGENT_LINK_BRIDGE_FILE"),
                        "AGENT_LINK_BINDING": os.environ.get("AGENT_LINK_BINDING"),
                    },
                },
                stream,
            )
    pending = []
    handshake_done = False
    for line in sys.stdin:
        line = line.rstrip("\n")
        if not line:
            continue
        try:
            record = json.loads(line)
        except ValueError:
            continue
        log(record)
        kind = record.get("type")

        if SCENARIO == "out_of_order":
            if not handshake_done and kind == "get_state":
                handshake_done = True
                respond(record, data=normal_data(record))
                continue
            pending.append(record)
            if len(pending) < 2:
                continue
            second, first = pending[1], pending[0]
            pending.clear()
            emit(
                {
                    "type": "message_update",
                    "assistantMessageEvent": {
                        "type": "text_delta",
                        "delta": f"async{LINE_SEP}event",
                    },
                }
            )
            emit(
                {
                    "id": second["id"],
                    "type": "response",
                    "command": second["type"],
                    "success": True,
                    "data": {"order": "second", **normal_data(second)},
                }
            )
            time.sleep(0.05)
            emit(
                {
                    "id": first["id"],
                    "type": "response",
                    "command": first["type"],
                    "success": True,
                    "data": {"order": "first", **normal_data(first)},
                }
            )
            continue

        if SCENARIO == "event_same_id" and kind != "get_state":
            emit(
                {
                    "id": record.get("id"),
                    "type": "message_update",
                    "assistantMessageEvent": {"type": "text_delta", "delta": "not-a-response"},
                }
            )
            time.sleep(0.15)
            respond(record, data=normal_data(record))
            continue

        if SCENARIO == "u2028" and kind != "get_state":
            emit(
                {
                    "type": "message_update",
                    "assistantMessageEvent": {"type": "text_delta", "delta": f"event{LINE_SEP}delta"},
                }
            )
            respond(record, data=normal_data(record))
            continue

        if SCENARIO == "false" and kind == "prompt":
            respond(record, success=False, error="simulated command rejection")
            continue

        if SCENARIO == "no_response" and kind in ("prompt", "steer", "follow_up"):
            continue

        if SCENARIO == "exit" and kind in ("prompt", "steer", "follow_up"):
            sys.exit(3)

        if SCENARIO == "delayed" and kind in ("prompt", "steer", "follow_up"):
            time.sleep(0.3)
            respond(record, data=normal_data(record))
            continue

        respond(record, data=normal_data(record))

        if SCENARIO == "bad_frame" and kind == "get_state":
            sys.stdout.write("this is not json\n")
            sys.stdout.flush()
        elif SCENARIO == "long_frame" and kind == "get_state":
            sys.stdout.write("x" * (8 * 1024 * 1024 + 1) + "\n")
            sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
