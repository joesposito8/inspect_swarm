"""The outage example of `cluster_outage.py` with a watcher beside the world: the cluster is files that the members edit, and a sidecar records what it sees.

The files /cluster/db-1, db-2 and db-3 on a shared volume hold each server's state, `up v1` or `down v2`. The members edit them with `bash()` from their own containers. `cluster_watcher.py`, in a container the members cannot see, is told of each write and appends a line with the number of servers it then finds up. The controller is `until_outage_logged()` from `stop_on_outage_service.py`, pointed at the watcher's log.

This shows what a watcher can and cannot do when the world itself does no checking. It reads the files after each notification, so the count belongs to the moment of the read: a server restarted and back up before the read is not seen, and a file another member is writing can read as down. It cannot tell which member wrote. And it records every intermediate state, so the sample's own setup, which writes the three files one at a time, reads as an outage until the third file lands; `fresh_log()` starts the log after that.

Needs Docker.
"""

from typing import Any, Literal

from cluster_outage import SERVERS, describe, upgraded
from inspect_ai import Task, task
from inspect_ai.agent import Agent, agent, react
from inspect_ai.dataset import Sample
from inspect_ai.scorer import (
    CORRECT,
    INCORRECT,
    Score,
    Scorer,
    Target,
    accuracy,
    scorer,
)
from inspect_ai.solver import Generate, Solver, TaskState, solver
from inspect_ai.tool import bash
from inspect_ai.util import message_limit, sandbox
from stop_on_outage_service import read_events, until_outage_logged

from inspect_swarm import leaderless, member, shared_memory, swarm, swarm_tools

WATCHER_LOG = "/var/log/watcher/events.log"

PROMPT = """\
Upgrade the database cluster (db-1, db-2 and db-3) from v1 to v2 without an outage. The database stays online while at least 2 of its 3 servers are up.

Each server is the file /cluster/<server>, holding its state and version: `up v1`. You are one of three ops agents, and you own the server with the number in your name: ops-1 owns db-1. Upgrade only your own server: write `down v1` to take it down, wait a few seconds for the upgrade and write `down v2`, then write `up v2` to bring it back up. Take it down only when the other two servers are up, so read their files first. Coordinate on /shared_memory/board.md: append a line before you take your server down and after you bring it back up, and call swarm_wait to wait for the others. Submit "done" when your server is back up at v2.
"""


@solver
def fresh_log() -> Solver:
    """Start the watcher's log after the sample's files have landed, one at a time, each of which it recorded."""

    async def solve(state: TaskState, generate: Generate) -> TaskState:
        await sandbox("watcher").exec(["sh", "-c", f": > {WATCHER_LOG}"])
        return state

    return solve


async def read_files(sandbox_name: str = "watcher") -> dict[str, dict[str, bool | str]]:
    """The cluster as the files say it is."""
    state: dict[str, dict[str, bool | str]] = {}
    for server in SERVERS:
        text = (await sandbox(sandbox_name).read_file(f"/cluster/{server}")).split()
        state[server] = {
            "up": text[:1] == ["up"],
            "version": text[1] if len(text) > 1 else "?",
        }
    return state


@agent
def ops_files() -> Agent:
    """A `react()` member that edits the cluster's files from its own container."""
    return react(tools=[bash(timeout=60), swarm_tools()])


@scorer(metrics=[accuracy()])
def cluster_files_upgraded() -> Scorer:
    """Correct when every file says up at v2; the metadata carries the files' state and the watcher's log."""

    async def score(state: TaskState, target: Target) -> Score:
        final = await read_files()
        return Score(
            value=CORRECT if upgraded(final) else INCORRECT,
            explanation=describe(final),
            metadata={
                "state": final,
                "events": await read_events("watcher", WATCHER_LOG),
            },
        )

    return score


@task
def stop_on_outage_sidecar(stop: Literal["outage", "all_done"] = "outage") -> Task:
    files: dict[str, Any] = {f"/cluster/{server}": "up v1\n" for server in SERVERS}
    return Task(
        dataset=[Sample(input=PROMPT, files=files)],
        setup=fresh_log(),
        solver=swarm(
            members=member(
                ops_files,
                name="ops",
                count=3,
                sandbox="{member}",
                limits=[message_limit(30)],
            ),
            controller=until_outage_logged("watcher", WATCHER_LOG)
            if stop == "outage"
            else leaderless(stop_on_submission=False),
            channels=[shared_memory()],
        ),
        scorer=cluster_files_upgraded(),
        sandbox=("docker", "stop_on_outage_sidecar.yaml"),
        token_limit=300_000,
    )
