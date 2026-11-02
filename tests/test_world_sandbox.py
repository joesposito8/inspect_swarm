"""The world log's file side: created root-only before any member starts, so a member running as another user cannot see it."""

from pathlib import Path
from typing import Any

import anyio
import pytest
from inspect_ai import Task, eval  # pyright: ignore[reportUnknownVariableType]
from inspect_ai.agent import Agent, AgentState, agent
from inspect_ai.dataset import Sample
from inspect_ai.log import EvalLog
from inspect_ai.solver import Generate, Solver, TaskState, solver
from inspect_ai.util import sandbox, store

from inspect_swarm import member, swarm
from inspect_swarm._world import WORLD_LOG_INTERVAL

from .test_world import SEEN, collecting

# python:3.12-slim runs as root and ships the `nobody` user (uid 65534)
_COMPOSE = """
services:
  default:
    image: python:3.12-slim
    command: tail -f /dev/null
    network_mode: none
"""

PROBES = {
    "list": "ls /swarm",
    "read": "cat /swarm/world.jsonl",
    "append": 'echo "{\\"forged\\": true}" >> /swarm/world.jsonl',
}


@agent
def world_log_prober() -> Agent:
    """Tries the log as `nobody`, then appends to it as root, as a world process would."""

    async def execute(state: AgentState) -> AgentState:
        probes: dict[str, Any] = {}
        for name, command in PROBES.items():
            result = await sandbox().exec(["sh", "-c", command], user="nobody")
            probes[name] = {"success": result.success, "stderr": result.stderr}
        store().set("probes", probes)
        await sandbox().exec(["sh", "-c", 'echo "{\\"a\\": 1}" >> /swarm/world.jsonl'])
        # stay running past the swarm's next read of the log, so the controller is
        # handed the record before this member's end ends its iterator
        await anyio.sleep(WORLD_LOG_INTERVAL + 0.5)
        modes = await sandbox().exec(
            ["stat", "-c", "%u %a", "/swarm", "/swarm/world.jsonl"]
        )
        store().set("modes", modes.stdout.split())
        return state

    return execute


def _run(tmp_path: Path, setup: Solver | None = None) -> EvalLog:
    compose = tmp_path / "compose.yaml"
    compose.write_text(_COMPOSE)
    task = Task(
        dataset=[Sample(input="x")],
        setup=setup,
        solver=swarm(
            member(world_log_prober(), name="writer"),
            controller=collecting(),
            channels=[],
        ),
        sandbox=("docker", str(compose)),
    )
    [log] = eval(
        task, model="mockllm/model", log_dir=str(tmp_path / "logs"), display="none"
    )
    return log


@pytest.mark.slow
def test_members_cannot_see_the_world_log(tmp_path: Path) -> None:
    log = _run(tmp_path)
    assert log.status == "success", log.error
    assert log.samples is not None
    [sample] = log.samples
    probes = sample.store["probes"]
    for name in PROBES:
        assert not probes[name]["success"], name
        assert "Permission denied" in probes[name]["stderr"], name
    # root-owned, and readable by root only
    assert sample.store["modes"] == ["0", "700", "0", "600"]
    # the root append is the one record the controller received, from the sandbox
    assert sample.store[SEEN] == [
        {"source": "sandbox", "record": {"a": 1}, "writer": "running"}
    ]


@pytest.mark.slow
def test_world_log_refuses_a_symlinked_directory(tmp_path: Path) -> None:
    @solver
    def symlink() -> Solver:
        async def solve(state: TaskState, generate: Generate) -> TaskState:
            await sandbox().exec(
                ["sh", "-c", "mkdir -p /tmp/elsewhere && ln -s /tmp/elsewhere /swarm"]
            )
            return state

        return solve

    log = _run(tmp_path, setup=symlink())
    assert log.status == "error"
    assert log.error is not None
    assert "/swarm exists and is not a directory" in log.error.message


@pytest.mark.slow
def test_world_log_keeps_an_existing_log(tmp_path: Path) -> None:
    @solver
    def early_line() -> Solver:
        async def solve(state: TaskState, generate: Generate) -> TaskState:
            await sandbox().exec(
                [
                    "sh",
                    "-c",
                    'mkdir -p /swarm && echo "{\\"a\\": 0}" >> /swarm/world.jsonl',
                ]
            )
            return state

        return solve

    log = _run(tmp_path, setup=early_line())
    assert log.status == "success", log.error
    assert log.samples is not None
    [sample] = log.samples
    # re-moded, not replaced: the earlier line comes first
    assert sample.store["modes"] == ["0", "700", "0", "600"]
    assert [s["record"] for s in sample.store[SEEN]] == [{"a": 0}, {"a": 1}]
