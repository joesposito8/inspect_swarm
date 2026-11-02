"""The outage example of `cluster_outage.py` with the cluster as a service in its own container.

`cluster_service.py` runs in the `cluster` container of `stop_on_outage_service.yaml`, the sample's default sandbox. It owns the state, checks quorum inside each change under its lock, and appends every change to the swarm's world log, `WORLD_LOG`, with the quorum at that moment and who asked for it. The members are `react()` agents with `bash()` in their own containers, which reach the service only through `python3 /opt/cluster ...` over an internal network: they cannot see the log, the check or the controller. The swarm reads the log from the host every second, and the controller is `until_recorded()` from `cluster_outage.py`, unchanged: the same stop, from a record the world wrote rather than the tools. `stop_on_outage_bridged.py` runs the same world with bridged members.

Needs Docker.
"""

import json
from typing import Any, Literal

from cluster_outage import State, describe, until_recorded, upgraded
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
    WORLD_LOG,
    leaderless,
    member,
    shared_memory,
    swarm,
    swarm_tools,
)

# where the service keeps its state, inside the `cluster` container; its log is WORLD_LOG
STATE = "/var/lib/cluster/state.json"

PROMPT = """\
Upgrade the database cluster (db-1, db-2 and db-3) from v1 to v2 without an outage. The database stays online while at least 2 of its 3 servers are up.

You are one of three ops agents, and you own the server with the number in your name: ops-1 owns db-1. Upgrade only your own server. The cluster is managed with the command `python3 /opt/cluster <action> <server>`, where the action is take_down, upgrade (takes a few seconds), bring_up or restart, and `python3 /opt/cluster status` shows which servers are up. Take your server down only when the other two servers are up, so check the status first. Coordinate on /shared_memory/board.md: append a line before you take your server down and after you bring it back up, and call swarm_wait to wait for the others. Submit "done" when your server is back up at v2.
"""


async def read_state(sandbox_name: str = "cluster") -> State:
    """The cluster as the service last saved it."""
    return json.loads(await sandbox(sandbox_name).read_file(STATE))


async def read_events(sandbox_name: str = "cluster") -> list[dict[str, Any]]:
    """Every change the service recorded, in order."""
    text = await sandbox(sandbox_name).read_file(WORLD_LOG)
    return [json.loads(line) for line in text.splitlines() if line]


# --- The members, the scorer and the task ---


@agent
def ops_bash() -> Agent:
    """A `react()` member that manages the cluster from its own container."""
    return react(tools=[bash(timeout=60), swarm_tools()])


@scorer(metrics=[accuracy()])
def cluster_upgraded_in(sandbox_name: str = "cluster") -> Scorer:
    """Correct when every server is up at v2 in the service's state; the metadata carries that state and the service's log."""

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
            controller=until_recorded()
            if stop == "outage"
            else leaderless(stop_on_submission=False),
            channels=[shared_memory()],
        ),
        scorer=cluster_upgraded_in(),
        sandbox=("docker", "stop_on_outage_service.yaml"),
        token_limit=300_000,
    )
