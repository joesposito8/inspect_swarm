"""The outage example of `cluster_outage.py` with a swarm event in place of the wake: `take_down()`, the only tool that can lose quorum, calls `swarm_notify()`, and the controller checks quorum at each event on `events()`."""

import functools
from contextlib import aclosing
from typing import Any, Literal

from cluster_outage import (
    PROMPT,
    bring_up,
    cluster_setup,
    cluster_status,
    cluster_upgraded,
    quorum_lost,
    take_down,
    upgrade,
)
from inspect_ai import Task, task
from inspect_ai.agent import Agent, agent, react
from inspect_ai.dataset import Sample
from inspect_ai.tool import Tool, ToolDef
from inspect_ai.util import message_limit

from inspect_swarm import (
    Controller,
    StopReason,
    SwarmControl,
    leaderless,
    member,
    shared_memory,
    swarm,
    swarm_controller,
    swarm_notify,
    swarm_tools,
)

# --- Notifying the controller ---


def notifying(tool: Tool) -> Tool:
    """`tool` unchanged, except that it notifies the controller each time a call to it returns."""
    tool_def = ToolDef(tool)

    # wraps() keeps the tool's signature, which Inspect reads to pass the arguments
    @functools.wraps(tool)
    async def execute(*args: Any, **kwargs: Any) -> Any:
        try:
            return await tool(*args, **kwargs)
        finally:
            swarm_notify()

    tool_def.tool = execute
    return tool_def.as_tool()


# --- The controller ---


@swarm_controller
def until_outage_event() -> Controller:
    """Start every member, and stop the swarm when the cluster loses quorum or every member has ended."""

    async def run(swarm: SwarmControl) -> StopReason:
        for m in swarm.members:
            swarm.start(m)
        # each event is a member that ended or may have changed the cluster
        async with aclosing(swarm.events()) as events:
            async for _ in events:
                if quorum_lost():
                    return "outage"
        return "all_done"

    return Controller(run)


# --- The members and the task ---


@agent
def ops_notifying(upgrade_seconds: float = 3.0) -> Agent:
    """A `react()` member whose `take_down()` notifies the controller."""
    return react(
        tools=[
            cluster_status(),
            notifying(take_down()),
            upgrade(upgrade_seconds),
            bring_up(),
            swarm_tools(),
        ]
    )


@task
def stop_on_outage_event(
    stop: Literal["outage", "all_done"] = "outage", upgrade_seconds: float = 3.0
) -> Task:
    return Task(
        dataset=[Sample(input=PROMPT)],
        setup=cluster_setup(),
        solver=swarm(
            # a message limit ends a member whose model never submits
            members=member(
                ops_notifying,
                args={"upgrade_seconds": upgrade_seconds},
                name="ops",
                count=3,
                limits=[message_limit(30)],
            ),
            controller=until_outage_event()
            if stop == "outage"
            else leaderless(stop_on_submission=False),
            channels=[shared_memory()],
        ),
        scorer=cluster_upgraded(),
        token_limit=300_000,
    )
