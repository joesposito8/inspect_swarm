"""The outage example of `cluster_outage.py`, technique 3: each tool checks quorum as it changes the cluster and records the loss in the store.

The check runs inside the change, in the code that owns the cluster, so a loss the cluster has already recovered from still stops the swarm. `restart()` is the case the other techniques miss: it takes a server down and brings it straight back up, and while another server is down for its upgrade the cluster is below quorum for an instant.
"""

from typing import Literal

import anyio
from cluster_outage import (
    OUTAGE,
    PROMPT,
    change,
    cluster,
    cluster_setup,
    cluster_status,
    cluster_upgraded,
    describe,
    member_acted,
    quorum_lost,
    until_outage,
)
from inspect_ai import Task, task
from inspect_ai.agent import Agent, agent, react
from inspect_ai.dataset import Sample
from inspect_ai.tool import Tool, ToolError, tool
from inspect_ai.util import message_limit, store

from inspect_swarm import leaderless, member, shared_memory, swarm, swarm_tools

# --- The tools: each change checks quorum and records a loss ---


def _apply(server: str, **fields: bool | str) -> str:
    """Change a server, check quorum right there, record a loss, and wake the controller."""
    state = change(server, **fields)
    if quorum_lost():
        store().set(OUTAGE, True)
    member_acted()
    return describe(state)


@tool
def take_down() -> Tool:
    async def execute(server: str) -> str:
        """Take a server down, so that it can be upgraded.

        Args:
            server: The server, such as db-1.
        """
        return _apply(server, up=False)

    return execute


@tool
def upgrade(seconds: float) -> Tool:
    async def execute(server: str) -> str:
        """Upgrade a server that is down to v2. Takes a few seconds.

        Args:
            server: The server, such as db-1.
        """
        if cluster().get(server, {}).get("up"):
            raise ToolError(f"{server} is up; take it down before upgrading it.")
        await anyio.sleep(seconds)
        return _apply(server, version="v2")

    return execute


@tool
def bring_up() -> Tool:
    async def execute(server: str) -> str:
        """Bring a server back up.

        Args:
            server: The server, such as db-1.
        """
        return _apply(server, up=True)

    return execute


@tool
def restart() -> Tool:
    async def execute(server: str) -> str:
        """Restart a server: take it down and bring it straight back up.

        Args:
            server: The server, such as db-1.
        """
        _apply(server, up=False)
        # Let other tasks run between the two changes, as a real restart would. A
        # controller that only wakes here looks after the server is back up and sees no
        # loss; the check above already recorded it.
        await anyio.sleep(0)
        return _apply(server, up=True)

    return execute


def world_tools(upgrade_seconds: float) -> list[Tool]:
    """The tools that read and change the cluster, each change checking quorum."""
    return [
        cluster_status(),
        take_down(),
        upgrade(upgrade_seconds),
        bring_up(),
        restart(),
    ]


# --- The members and the task ---


@agent
def ops_checked(upgrade_seconds: float = 3.0) -> Agent:
    """A `react()` member whose cluster tools record a loss of quorum as they make it."""
    return react(tools=[*world_tools(upgrade_seconds), swarm_tools()])


@task
def stop_on_outage_world(
    stop: Literal["outage", "all_done"] = "outage", upgrade_seconds: float = 3.0
) -> Task:
    return Task(
        dataset=[Sample(input=PROMPT)],
        setup=cluster_setup(),
        solver=swarm(
            # a message limit ends a member whose model never submits
            members=member(
                ops_checked,
                args={"upgrade_seconds": upgrade_seconds},
                name="ops",
                count=3,
                limits=[message_limit(30)],
            ),
            controller=until_outage(latched=True)
            if stop == "outage"
            else leaderless(stop_on_submission=False),
            channels=[shared_memory()],
        ),
        scorer=cluster_upgraded(),
        token_limit=300_000,
    )
