"""Stop a swarm on the state of the world: halt a rolling upgrade when a database cluster loses quorum.

Three ops agents each upgrade one server of a three-server database cluster. The database stays online while at least two servers are up, so each agent is told to take its server down only when the other two are up. If two agents check at the same time, both see three up and both take theirs down, leaving one. Neither broke the rule; acting at the same time caused the outage.

`until_outage()` stops the swarm the moment fewer than two servers are up, with the stop reason `controller:outage`, as a deployment tool halts a rollout when service health drops below a threshold. The run ends cleanly through the controller, with the grace period.

Only the controller decides when to stop: it checks quorum each time a member may have changed the cluster. There are two different techniques done:
1. `waking()` wraps each cluster tool and wakes the controller whenever an agent makes a designated tool call.
2. `stop_on_outage_turns.py` wakes the same controller from `react(on_continue=...)` instead. This depends on Inspect native agents, bridged CLI agents wouldn't be able to do this as designed.

Neither notices a change made outside these tools, such as one a bridged CLI agent makes from its own shell, and neither sees a loss of quorum that is over by the time the controller looks, such as a server restarted while another is down.

Two more techniques check quorum inside the change itself, in the code that owns the cluster, and record the loss there, so the controller cannot look too late:
3. `stop_on_outage_world.py` keeps the cluster in the store, as here, but its tools check quorum as they change it and set `outage` in the store. `until_outage(latched=True)` waits for that record.
4. `stop_on_outage_service.py` moves the cluster into a container that checks every change the same way and logs it. The controller blocks on that log, so the technique works for any member that can reach the container, bridged CLI agents included (`stop_on_outage_bridged.py`). `stop_on_outage_sidecar.py` shows what a watcher beside the world, rather than in it, can and cannot see.
"""

from contextlib import aclosing

import anyio
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
from inspect_ai.tool import Tool, ToolError, tool
from inspect_ai.util import store

from inspect_swarm import Controller, StopReason, SwarmControl, swarm_controller

# --- The cluster ---

SERVERS = ["db-1", "db-2", "db-3"]
QUORUM = 2
# the store key a tool sets when a change it made lost quorum (technique 3)
OUTAGE = "outage"

PROMPT = """\
Upgrade the database cluster (db-1, db-2 and db-3) from v1 to v2 without an outage. The database stays online while at least 2 of its 3 servers are up.

You are one of three ops agents, and you own the server with the number in your name: ops-1 owns db-1. Upgrade only your own server: take it down, upgrade it, then bring it back up. Take it down only when the other two servers are up, so check cluster_status first. Coordinate on /shared_memory/board.md: append a line before you take your server down and after you bring it back up, and call swarm_wait to wait for the others. Submit "done" when your server is back up at v2.
"""


def cluster() -> dict[str, dict[str, bool | str]]:
    """The cluster as it is now: each server's `up` and `version`."""
    return dict(store().get("cluster"))


def describe(state: dict[str, dict[str, bool | str]]) -> str:
    """One line per cluster, as the tools report it: `db-1 up v1, db-2 down v1, db-3 up v1`."""
    return ", ".join(
        f"{server} {'up' if s['up'] else 'down'} {s['version']}"
        for server, s in state.items()
    )


def upgraded(state: dict[str, dict[str, bool | str]]) -> bool:
    """Whether every server is up at v2."""
    return all(s["up"] and s["version"] == "v2" for s in state.values())


def change(server: str, **change: bool | str) -> dict[str, dict[str, bool | str]]:
    """Change one server's `up` or `version` in the store, and return the cluster after it."""
    state = cluster()
    if server not in state:
        raise ToolError(f"No server {server!r}; the servers are {', '.join(state)}.")
    state[server] = {**state[server], **change}
    store().set("cluster", state)
    return state


@tool
def cluster_status() -> Tool:
    async def execute() -> str:
        """Show which servers of the cluster are up, and their versions."""
        return describe(cluster())

    return execute


@tool
def take_down() -> Tool:
    async def execute(server: str) -> str:
        """Take a server down, so that it can be upgraded.

        Args:
            server: The server, such as db-1.
        """
        return describe(change(server, up=False))

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
        return describe(change(server, version="v2"))

    return execute


@tool
def bring_up() -> Tool:
    async def execute(server: str) -> str:
        """Bring a server back up.

        Args:
            server: The server, such as db-1.
        """
        return describe(change(server, up=True))

    return execute


def cluster_tools(upgrade_seconds: float) -> list[Tool]:
    """The tools that read and change the cluster."""
    return [cluster_status(), take_down(), upgrade(upgrade_seconds), bring_up()]


@solver
def cluster_setup() -> Solver:
    """Start every server up at v1, with no outage recorded."""

    async def solve(state: TaskState, generate: Generate) -> TaskState:
        state.store.set(
            "cluster", {server: {"up": True, "version": "v1"} for server in SERVERS}
        )
        state.store.set(OUTAGE, False)
        return state

    return solve


@scorer(metrics=[accuracy()])
def cluster_upgraded() -> Scorer:
    """Correct when every server is up at v2."""

    async def score(state: TaskState, target: Target) -> Score:
        final = state.store.get("cluster")
        return Score(
            value=CORRECT if upgraded(final) else INCORRECT, explanation=describe(final)
        )

    return score


# --- Waking the controller ---


class _Wake:
    """Wakes the controller, as often as it is set: each `set()` releases the current `wait()`."""

    def __init__(self) -> None:
        self._event = anyio.Event()

    def set(self) -> None:
        self._event.set()
        self._event = anyio.Event()

    async def wait(self) -> None:
        await self._event.wait()


_wakes: dict[int, _Wake] = {}


def member_acted() -> None:
    """Wake the controller of the caller's sample, if one is listening."""
    wake = _wakes.get(id(store()))
    if wake is not None:
        wake.set()


# --- The controller ---


def quorum_lost() -> bool:
    """Whether fewer than `QUORUM` servers are up right now."""
    return sum(bool(s["up"]) for s in cluster().values()) < QUORUM


def outage_latched() -> bool:
    """Whether a tool has recorded that a change of its lost quorum, even if the cluster has recovered since."""
    return bool(store().get(OUTAGE, False))


@swarm_controller
def until_outage(latched: bool = False) -> Controller:
    """Start every member, and stop the swarm when the cluster loses quorum or every member has ended.

    With `latched`, the controller stops on the record a tool left in the store at the moment of its change (technique 3), rather than on the cluster as it is when the controller looks.
    """
    lost = outage_latched if latched else quorum_lost

    async def run(swarm: SwarmControl) -> StopReason:
        wake = _wakes[id(store())] = _Wake()
        try:
            for m in swarm.members:
                swarm.start(m)
            async with anyio.create_task_group() as tg:

                async def every_member_ends() -> None:
                    async with aclosing(swarm.events()) as events:
                        async for _ in events:
                            pass
                    tg.cancel_scope.cancel()

                tg.start_soon(every_member_ends)
                # checked again each time a member may have changed the cluster
                while not lost():
                    await wake.wait()
                tg.cancel_scope.cancel()
            return "outage" if lost() else "all_done"
        finally:
            del _wakes[id(store())]

    return Controller(run)
