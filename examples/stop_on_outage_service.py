"""The outage example of `stop_on_outage.py` with the cluster as a service in its own container.

`cluster_service.py` runs in the `cluster` container of `stop_on_outage_service.yaml`. It owns the state, takes a snapshot of the cluster inside each change under its lock, and reports every change to the swarm on the host through the `world` sandbox service that `world_service(sandbox="cluster")` publishes before the swarm starts, with the servers up at that moment and who asked for it. The members are `react()` agents with `bash()` in their own containers, which reach the service only with `curl` over an internal network: they cannot see the reports, the service's client or the controller. The controller is `until_outage()` from `stop_on_outage.py`, unchanged: the same stop, from a report the world made rather than the tools.

Needs Docker.
"""

import json
from typing import Literal

from inspect_ai import Task, task
from inspect_ai.agent import Agent, agent, as_solver, react
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
from stop_on_outage import KIND, State, describe, until_outage, upgraded

from inspect_swarm import (
    artifact_result,
    leaderless,
    member,
    shared_memory,
    swarm,
    swarm_tools,
    world_reports,
    world_service,
)

# where the service keeps its state, inside the `cluster` container
STATE = "/var/lib/cluster/state.json"

PROMPT = """\
Upgrade the database cluster (db-1, db-2 and db-3) from v1 to v2 without an outage. The database stays online while at least 2 of its 3 servers are up.

You are one of three ops agents, and you own the server with the number in your name: ops-1 owns db-1. Upgrade only your own server. The cluster is managed over HTTP: `curl -sS -X POST http://cluster:8000/<action>/<server>`, where the action is take_down, upgrade (takes a few seconds), bring_up or restart, and `curl -sS http://cluster:8000/status` shows which servers are up. Take your server down only when the other two servers are up, so check the status first. Coordinate on /shared_memory/board.md: append a line before you take your server down and after you bring it back up, and call swarm_wait to wait for the others. Submit "done" when your server is back up at v2.
"""


async def read_state(sandbox_name: str = "cluster") -> State:
    """The cluster as the service last saved it."""
    return json.loads(await sandbox(sandbox_name).read_file(STATE))


# --- The members, the scorer and the task ---


@agent
def ops_bash() -> Agent:
    """A `react()` member that manages the cluster from its own container."""
    return react(tools=[bash(timeout=60), swarm_tools()])


@scorer(metrics=[accuracy()])
def cluster_upgraded_in(sandbox_name: str = "cluster") -> Scorer:
    """Correct when every server is up at v2 in the service's state; the metadata carries that state and every cluster report."""

    async def score(state: TaskState, target: Target) -> Score:
        final = await read_state(sandbox_name)
        reports = [dict(r.report) for r in world_reports(state, kind=KIND)]
        return Score(
            value=CORRECT if upgraded(final) else INCORRECT,
            explanation=describe(final),
            metadata={"state": final, "reports": reports},
        )

    return score


@task
def stop_on_outage_service(stop: Literal["outage", "all_done"] = "outage") -> Task:
    return Task(
        dataset=[Sample(input=PROMPT)],
        solver=[
            # the service's reports reach the host from here on
            world_service(sandbox="cluster"),
            as_solver(
                swarm(
                    # a message limit ends a member whose model never submits
                    members=member(
                        ops_bash,
                        name="ops",
                        count=3,
                        sandbox="{member}",
                        limits=[message_limit(30)],
                    ),
                    controller=until_outage()
                    if stop == "outage"
                    else leaderless(stop="end"),
                    channels=[shared_memory()],
                    result=artifact_result(),
                )
            ),
        ],
        scorer=cluster_upgraded_in(),
        sandbox=("docker", "stop_on_outage_service.yaml"),
        token_limit=300_000,
    )
