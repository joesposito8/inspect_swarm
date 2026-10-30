"""The cluster as a service, run inside the `cluster` container of stop_on_outage_service.yaml.

It owns the cluster's state and checks quorum inside every change, under its lock, before anything else can run. Each change is appended to an event log, with the quorum as it was at that change and who made it, and the current state is written to a file. The controller on the host reads both; the agents can only call the API.

    POST /take_down/db-1   POST /upgrade/db-1   POST /bring_up/db-1   POST /restart/db-1   GET /status

Standard library only, so it runs on a plain python image.
"""

import json
import os
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

SERVERS = ["db-1", "db-2", "db-3"]
QUORUM = 2
UPGRADE_SECONDS = float(os.environ.get("UPGRADE_SECONDS", "2"))
LOG = "/var/lib/cluster/events.log"
STATE = "/var/lib/cluster/state.json"

state: dict[str, dict[str, bool | str]] = {
    server: {"up": True, "version": "v1"} for server in SERVERS
}
lock = threading.Lock()


def describe() -> str:
    return ", ".join(
        f"{server} {'up' if s['up'] else 'down'} {s['version']}"
        for server, s in state.items()
    )


def record(action: str, server: str, by: str) -> None:
    """Append the change to the log, with the quorum as it is at this moment, and save the state."""
    up = sum(1 for s in state.values() if s["up"])
    line = {
        "t": time.time(),
        "action": action,
        "server": server,
        "by": by,
        "up": up,
        "outage": up < QUORUM,
    }
    with open(LOG, "a") as log:
        log.write(json.dumps(line) + "\n")
    with open(STATE, "w") as f:
        json.dump(state, f)


def change(action: str, server: str, by: str) -> tuple[int, str]:
    """Apply one action and return the HTTP status and the cluster after it."""
    if server not in state:
        return 404, f"error: no server {server}; the servers are {', '.join(SERVERS)}"
    if action == "upgrade":
        with lock:
            if state[server]["up"]:
                return 409, f"error: {server} is up; take it down before upgrading it"
        time.sleep(UPGRADE_SECONDS)
        with lock:
            state[server]["version"] = "v2"
            record(action, server, by)
            return 200, describe()
    with lock:
        if action == "take_down":
            state[server]["up"] = False
        elif action == "bring_up":
            state[server]["up"] = True
        elif action == "restart":
            # down, recorded, then straight back up: the log keeps the instant below quorum
            state[server]["up"] = False
            record("restart:down", server, by)
            state[server]["up"] = True
        else:
            return 404, f"error: no action {action}"
        record(action, server, by)
        return 200, describe()


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        with lock:
            self._reply(200, describe())

    def do_POST(self) -> None:
        _, action, server = self.path.split("/", 2)
        self._reply(*change(action, server, self._caller()))

    def _caller(self) -> str:
        """The caller's network name, resolved here rather than taken from the request."""
        try:
            return socket.gethostbyaddr(self.client_address[0])[0]
        except OSError:
            return self.client_address[0]

    def _reply(self, status: int, text: str) -> None:
        body = text.encode()
        self.send_response(status)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: Any) -> None:
        pass


os.makedirs(os.path.dirname(LOG), exist_ok=True)
open(LOG, "a").close()
with lock:
    with open(STATE, "w") as f:
        json.dump(state, f)
ThreadingHTTPServer(("0.0.0.0", 8000), Handler).serve_forever()
