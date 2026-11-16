"""The cluster as a service, run inside the `cluster` container of stop_on_outage_service.yaml.

It owns the cluster's state and takes a snapshot of it inside every change, under its lock, before anything else can run. Each snapshot is reported to the swarm on the host through the `world` sandbox service that `world_service(sandbox="cluster")` publishes in this container, with the servers up at that change and who made it, and the current state is written to a file. The report is sent after the lock is released, because the call blocks until the host has recorded it, and in order, so a restart's instant below quorum is reported before its recovery. The swarm on the host hears every report; the scorer reads the state file and the reports; the agents can only call the API.

    POST /take_down/db-1   POST /upgrade/db-1   POST /bring_up/db-1   POST /restart/db-1   GET /status

Standard library only, so it runs on a plain python image.
"""

import json
import os
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, cast

SERVERS = ["db-1", "db-2", "db-3"]
UPGRADE_SECONDS = float(os.environ.get("UPGRADE_SECONDS", "2"))
STATE = "/var/lib/cluster/state.json"
# the client of the swarm's `world` service, written by world_service() on the host
# before any member starts
CLIENT_DIR = "/var/tmp/sandbox-services/world"

state: dict[str, dict[str, bool | str]] = {
    server: {"up": True, "version": "v1"} for server in SERVERS
}
lock = threading.Lock()


def describe() -> str:
    return ", ".join(
        f"{server} {'up' if s['up'] else 'down'} {s['version']}"
        for server, s in state.items()
    )


def snapshot(action: str, server: str, by: str) -> dict[str, Any]:
    """The report of a change, taken under the lock: the servers up at this moment."""
    return {
        "action": action,
        "server": server,
        "by": by,
        "up": sum(1 for s in state.values() if s["up"]),
    }


def save() -> None:
    with open(STATE, "w") as f:
        json.dump(state, f)


def report(**fields: Any) -> None:
    """Send one report to the swarm; blocks for the round trip, so never under the lock."""
    if CLIENT_DIR not in sys.path:
        sys.path.append(CLIENT_DIR)
    import world  # pyright: ignore[reportMissingImports]

    cast(Any, world).call_world("world_report", kind="cluster", **fields)


def change(action: str, server: str, by: str) -> tuple[int, str]:
    """Apply one action, report it, and return the HTTP status and the cluster after it."""
    if server not in state:
        return 404, f"error: no server {server}; the servers are {', '.join(SERVERS)}"
    reports: list[dict[str, Any]] = []
    if action == "upgrade":
        with lock:
            if state[server]["up"]:
                return 409, f"error: {server} is up; take it down before upgrading it"
        time.sleep(UPGRADE_SECONDS)
        with lock:
            state[server]["version"] = "v2"
            reports.append(snapshot(action, server, by))
            save()
            text = describe()
    else:
        with lock:
            if action == "take_down":
                state[server]["up"] = False
            elif action == "bring_up":
                state[server]["up"] = True
            elif action == "restart":
                # down, snapshotted, then straight back up: the reports keep the
                # instant below quorum
                state[server]["up"] = False
                reports.append(snapshot("restart:down", server, by))
                state[server]["up"] = True
            else:
                return 404, f"error: no action {action}"
            reports.append(snapshot(action, server, by))
            save()
            text = describe()
    for fields in reports:
        report(**fields)
    return 200, text


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


os.makedirs(os.path.dirname(STATE), exist_ok=True)
with lock:
    save()
ThreadingHTTPServer(("0.0.0.0", 8000), Handler).serve_forever()
