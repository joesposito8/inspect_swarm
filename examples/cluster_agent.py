"""A stand-in for a CLI agent, mounted at /opt/agent.py in each ops container (stop_on_outage_bridged.py).

Like a real CLI agent run through Inspect's agent bridge, it talks to the model through the bridge's OpenAI-compatible endpoint and runs its commands itself, inside the container, so Inspect sees its model calls but none of its tool calls. The conversation so far arrives as JSON on stdin.
"""

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from typing import Any

BASE = os.environ["OPENAI_BASE_URL"]
TOOL = {
    "type": "function",
    "function": {
        "name": "cluster",
        "description": "Run a cluster command: status, take_down, upgrade, bring_up or restart.",
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["status", "take_down", "upgrade", "bring_up", "restart"],
                },
                "server": {
                    "type": "string",
                    "description": "The server, such as db-1.",
                },
            },
            "required": ["action"],
        },
    },
}


def complete(messages: list[dict[str, Any]]) -> dict[str, Any]:
    payload = json.dumps(
        {"model": "inspect", "messages": messages, "tools": [TOOL]}
    ).encode()
    request = urllib.request.Request(
        BASE + "/chat/completions",
        data=payload,
        headers={"Content-Type": "application/json"},
    )
    for _ in range(20):
        try:
            with urllib.request.urlopen(request) as response:
                return json.load(response)["choices"][0]["message"]
        except urllib.error.URLError as error:
            if isinstance(error, urllib.error.HTTPError):
                raise
            # the bridge may still be starting
            time.sleep(0.5)
    sys.exit("error: the model endpoint is not answering")


def run(arguments: dict[str, Any]) -> str:
    command = ["python3", "/opt/cluster", str(arguments["action"])]
    if arguments["action"] != "status":
        command.append(str(arguments.get("server", "")))
    result = subprocess.run(command, capture_output=True, text=True)
    return result.stdout + result.stderr


messages: list[dict[str, Any]] = json.load(sys.stdin)
while True:
    message = complete(messages)
    messages.append(message)
    calls: list[dict[str, Any]] = message.get("tool_calls") or []
    if not calls:
        print(message.get("content") or "")
        break
    for call in calls:
        output = run(json.loads(call["function"]["arguments"]))
        messages.append({"role": "tool", "tool_call_id": call["id"], "content": output})
