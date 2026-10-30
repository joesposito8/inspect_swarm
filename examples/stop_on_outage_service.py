"""The outage example of `cluster_outage.py`, technique 4: the cluster is a service in its own container, and it checks quorum on every change.

`cluster_service.py` runs in the `cluster` container of `stop_on_outage_service.yaml`. It owns the state, checks quorum inside each change under its lock, and appends every change to an event log with the quorum at that moment and who made it. The members are `react()` agents with `bash()` in their own containers, which reach the service only through `python3 /opt/cluster ...` over an internal network: they cannot see the log, the check or the controller. The controller, `until_outage_logged()`, blocks on the log from the host and stops the moment a change below quorum is recorded, whatever the member that made it is: `stop_on_outage_bridged.py` runs the same controller with bridged members.

Needs Docker.
"""

import json
from contextlib import aclosing
from typing import Any, Literal

import anyio
from cluster_outage import describe, upgraded
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
from inspect_ai.solver import TaskState
from inspect_ai.tool import bash
from inspect_ai.util import message_limit, sandbox

from inspect_swarm import (
    Controller,
    StopReason,
    SwarmControl,
    leaderless,
    member,
    shared_memory,
    swarm,
    swarm_controller,
    swarm_tools,
)

# where the service keeps its log and state, inside the `cluster` container
EVENTS = "/var/lib/cluster/events.log"
STATE = "/var/lib/cluster/state.json"

PROMPT = """\
Upgrade the database cluster (db-1, db-2 and db-3) from v1 to v2 without an outage. The database stays online while at least 2 of its 3 servers are up.

You are one of three ops agents, and you own the server with the number in your name: ops-1 owns db-1. Upgrade only your own server. The cluster is managed with the command `python3 /opt/cluster <action> <server>`, where the action is take_down, upgrade (takes a few seconds), bring_up or restart, and `python3 /opt/cluster status` shows which servers are up. Take your server down only when the other two servers are up, so check the status first. Coordinate on /shared_memory/board.md: append a line before you take your server down and after you bring it back up, and call swarm_wait to wait for the others. Submit "done" when your server is back up at v2.
"""


async def read_state(sandbox_name: str = "cluster") -> dict[str, dict[str, bool | str]]:
    """The cluster as the service last saved it."""
    return json.loads(await sandbox(sandbox_name).read_file(STATE))


async def read_events(
    sandbox_name: str = "cluster", log: str = EVENTS
) -> list[dict[str, Any]]:
    """Every change the world recorded, in order."""
    text = await sandbox(sandbox_name).read_file(log)
    return [json.loads(line) for line in text.splitlines() if line]


# --- The controller ---


@swarm_controller
def until_outage_logged(sandbox_name: str = "cluster", log: str = EVENTS) -> Controller:
    """Start every member, and stop the swarm when the world's log records a change below quorum, or every member has ended.

    The wait is a blocking command in the world's container that returns at the first logged outage. The host side is cancelled if every member ends first; the command it leaves behind ends with the container. Starting that command takes a moment, so when the members end first the log is read once more: an outage recorded before the wait began is still an outage.
    """
    # GNU tail notices when grep has exited; BusyBox tail (alpine images) does not
    wait = ["sh", "-c", f"tail -n +1 -F {log} | grep -q -m1 '\"outage\": true'"]

    async def run(swarm: SwarmControl) -> StopReason:
        for m in swarm.members:
            swarm.start(m)
        outage = False
        async with anyio.create_task_group() as tg:

            async def every_member_ends() -> None:
                async with aclosing(swarm.events()) as events:
                    async for _ in events:
                        pass
                tg.cancel_scope.cancel()

            async def outage_logged() -> None:
                nonlocal outage
                await sandbox(sandbox_name).exec(wait, timeout=None, concurrency=False)
                outage = True
                tg.cancel_scope.cancel()

            tg.start_soon(every_member_ends)
            tg.start_soon(outage_logged)
        if not outage:
            outage = any(e["outage"] for e in await read_events(sandbox_name, log))
        return "outage" if outage else "all_done"

    return Controller(run)


# --- The members, the scorer and the task ---


@agent
def ops_bash() -> Agent:
    """A `react()` member that manages the cluster from its own container."""
    return react(tools=[bash(timeout=60), swarm_tools()])


@scorer(metrics=[accuracy()])
def cluster_upgraded_in(sandbox_name: str = "cluster") -> Scorer:
    """Correct when every server is up at v2 in the service's state; the metadata carries that state and the world's log."""

    async def score(state: TaskState, target: Target) -> Score:
        final = await read_state(sandbox_name)
        return Score(
            value=CORRECT if upgraded(final) else INCORRECT,
            explanation=describe(final),
            metadata={"state": final, "events": await read_events(sandbox_name)},
        )

    return score


@task
def stop_on_outage_service(stop: Literal["outage", "all_done"] = "outage") -> Task:
    return Task(
        dataset=[Sample(input=PROMPT)],
        solver=swarm(
            # a message limit ends a member whose model never submits
            members=member(
                ops_bash,
                name="ops",
                count=3,
                sandbox="{member}",
                limits=[message_limit(30)],
            ),
            controller=until_outage_logged()
            if stop == "outage"
            else leaderless(stop_on_submission=False),
            channels=[shared_memory()],
        ),
        scorer=cluster_upgraded_in(),
        sandbox=("docker", "stop_on_outage_service.yaml"),
        token_limit=300_000,
    )
