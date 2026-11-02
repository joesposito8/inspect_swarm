"""The outage example of `cluster_outage.py` with the cluster in the sample's store.

The tools are the only way to change the cluster, and each one records its change, with the quorum after it, through `swarm_record()`. The controller receives each record as a `WorldChanged` event before the tool returns, and `until_recorded()` stops the swarm at the first one that records an outage. A tool call in flight finishes before the member stops, so the upgrade that was running ends, but its server never comes back up.

Run with `-T stop=all_done` to let the swarm run to the end whatever happens to the cluster.
"""

from typing import Literal

from cluster_outage import (
    PROMPT,
    bring_up,
    cluster_setup,
    cluster_status,
    cluster_upgraded,
    restart,
    take_down,
    until_recorded,
    upgrade,
)
from inspect_ai import Task, task
from inspect_ai.agent import Agent, agent, react
from inspect_ai.dataset import Sample
from inspect_ai.util import message_limit

from inspect_swarm import leaderless, member, shared_memory, swarm, swarm_tools

# --- The members and the task ---


@agent
def ops(upgrade_seconds: float = 3.0) -> Agent:
    """A `react()` member with the cluster tools."""
    return react(
        tools=[
            cluster_status(),
            take_down(),
            upgrade(upgrade_seconds),
            bring_up(),
            restart(),
            swarm_tools(),
        ]
    )


@task
def stop_on_outage_store(
    stop: Literal["outage", "all_done"] = "outage", upgrade_seconds: float = 3.0
) -> Task:
    return Task(
        dataset=[Sample(input=PROMPT)],
        setup=cluster_setup(),
        solver=swarm(
            # a message limit ends a member whose model never submits
            members=member(
                ops,
                args={"upgrade_seconds": upgrade_seconds},
                name="ops",
                count=3,
                limits=[message_limit(30)],
            ),
            controller=until_recorded()
            if stop == "outage"
            else leaderless(stop_on_submission=False),
            channels=[shared_memory()],
        ),
        scorer=cluster_upgraded(),
        token_limit=300_000,
    )
