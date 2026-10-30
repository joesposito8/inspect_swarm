"""The outage example of `cluster_outage.py`, technique 1: `waking()` wraps `take_down()`, the only tool that can lose quorum, to wake the controller."""

import functools
from typing import Any, Literal

from cluster_outage import (
    PROMPT,
    bring_up,
    cluster_setup,
    cluster_status,
    cluster_upgraded,
    member_acted,
    take_down,
    until_outage,
    upgrade,
)
from inspect_ai import Task, task
from inspect_ai.agent import Agent, agent, react
from inspect_ai.dataset import Sample
from inspect_ai.tool import Tool, ToolDef
from inspect_ai.util import message_limit

from inspect_swarm import leaderless, member, shared_memory, swarm, swarm_tools

# --- Waking the controller ---


def waking(tool: Tool) -> Tool:
    """`tool` unchanged, except that it wakes the controller each time a call to it returns."""
    tool_def = ToolDef(tool)

    # wraps() keeps the tool's signature, which Inspect reads to pass the arguments
    @functools.wraps(tool)
    async def execute(*args: Any, **kwargs: Any) -> Any:
        try:
            return await tool(*args, **kwargs)
        finally:
            member_acted()

    tool_def.tool = execute
    return tool_def.as_tool()


# --- The members and the task ---


@agent
def ops(upgrade_seconds: float = 3.0) -> Agent:
    """A `react()` member whose `take_down()` wakes the controller."""
    return react(
        tools=[
            cluster_status(),
            waking(take_down()),
            upgrade(upgrade_seconds),
            bring_up(),
            swarm_tools(),
        ]
    )


@task
def stop_on_outage_tools(
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
            controller=until_outage()
            if stop == "outage"
            else leaderless(stop_on_submission=False),
            channels=[shared_memory()],
        ),
        scorer=cluster_upgraded(),
        token_limit=300_000,
    )
