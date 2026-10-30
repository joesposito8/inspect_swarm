"""The outage example of `cluster_outage.py`, technique 2: `react(on_continue=...)` wakes the controller after each turn."""

from typing import Literal

from cluster_outage import (
    PROMPT,
    cluster_setup,
    cluster_tools,
    cluster_upgraded,
    member_acted,
    until_outage,
)
from inspect_ai import Task, task
from inspect_ai.agent import Agent, AgentState, agent, react
from inspect_ai.dataset import Sample
from inspect_ai.util import message_limit

from inspect_swarm import leaderless, member, shared_memory, swarm, swarm_tools

# --- Waking the controller ---


async def _after_turn(state: AgentState) -> bool:
    member_acted()
    # react's default: continue, with a continue prompt only when the turn made no tool calls
    return True


# --- The members and the task ---


@agent
def ops_on_continue(upgrade_seconds: float = 3.0) -> Agent:
    """A `react()` member that wakes the controller after each turn."""
    return react(
        tools=[*cluster_tools(upgrade_seconds), swarm_tools()],
        on_continue=_after_turn,
    )


@task
def stop_on_outage_turns(
    stop: Literal["outage", "all_done"] = "outage", upgrade_seconds: float = 3.0
) -> Task:
    return Task(
        dataset=[Sample(input=PROMPT)],
        setup=cluster_setup(),
        solver=swarm(
            # a message limit ends a member whose model never submits
            members=member(
                ops_on_continue,
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
