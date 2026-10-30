"""The outage example of `cluster_outage.py`, technique 4 with bridged members: the same service world as `stop_on_outage_service.py`, managed by CLI agents.

Each member runs `cluster_agent.py` in its own container through Inspect's agent bridge: the agent gets its model calls through the bridge and runs its commands itself, so Inspect records its model calls but none of its tool calls. The service still checks quorum on every change it is asked for, and the controller still blocks on the service's log, so the swarm stops the same way with no change to the world or the controller. Bridged members cannot use the shared-memory board, so these coordinate by checking the status.

Needs Docker, and the `openai` package on the host for the bridge's OpenAI-compatible endpoint.
"""

import json
from typing import Literal

from inspect_ai import Task, task
from inspect_ai.agent import Agent, AgentState, agent, sandbox_agent_bridge
from inspect_ai.dataset import Sample
from inspect_ai.util import message_limit, sandbox
from stop_on_outage_service import cluster_upgraded_in, until_outage_logged

from inspect_swarm import leaderless, member, swarm

PROMPT = """\
Upgrade the database cluster (db-1, db-2 and db-3) from v1 to v2 without an outage. The database stays online while at least 2 of its 3 servers are up.

You are one of three ops agents, and you own the server with the number in your name: ops-1 owns db-1. Upgrade only your own server: take it down, upgrade it (takes a few seconds), then bring it back up, with the cluster tool. Take it down only when the other two servers are up, so check the status first, and check it again while you wait for the others. Answer "done" when your server is back up at v2.
"""


@agent
def ops_cli() -> Agent:
    """A member that is a CLI agent in its own container, bridged to the swarm's model."""

    async def execute(state: AgentState) -> AgentState:
        conversation = json.dumps(
            [{"role": m.role, "content": m.text} for m in state.messages]
        )
        async with sandbox_agent_bridge(state, sandbox="default") as bridge:
            result = await sandbox().exec(
                ["python3", "/opt/agent.py"],
                input=conversation,
                env={"OPENAI_BASE_URL": "http://localhost:13131/v1"},
                timeout=600,
            )
            if not result.success:
                raise RuntimeError(result.stderr)
        return bridge.state

    return execute


@task
def stop_on_outage_bridged(stop: Literal["outage", "all_done"] = "outage") -> Task:
    return Task(
        dataset=[Sample(input=PROMPT)],
        solver=swarm(
            members=member(
                ops_cli,
                name="ops",
                count=3,
                sandbox="{member}",
                bridged=True,
                limits=[message_limit(30)],
            ),
            controller=until_outage_logged()
            if stop == "outage"
            else leaderless(stop_on_submission=False),
            # bridged members cannot use bus channels such as shared_memory()
            channels=[],
        ),
        scorer=cluster_upgraded_in(),
        sandbox=("docker", "stop_on_outage_service.yaml"),
        token_limit=300_000,
    )
