"""World reports from a sandbox: the service `world_service()` publishes, who can call it, and when it stops."""

from pathlib import Path
from typing import Any

import pytest
from inspect_ai import Task, eval  # pyright: ignore[reportUnknownVariableType]
from inspect_ai.agent import Agent, AgentState, agent, as_solver
from inspect_ai.dataset import Sample
from inspect_ai.event import LoggerEvent, SwarmEvent
from inspect_ai.log import EvalLog
from inspect_ai.scorer import Score, Scorer, Target, scorer
from inspect_ai.solver import Solver, TaskState
from inspect_ai.util import sandbox, store

from inspect_swarm import (
    member,
    swarm,
    swarm_result,
    users,
    world_reports,
    world_service,
)

from .test_world import SEEN, collecting

# python:3.12-slim runs as root and ships the `nobody` user; alpine has no python3
_COMPOSE = """
services:
  default:
    image: {image}
    command: tail -f /dev/null
    network_mode: none
"""

CLIENT_DIR = "/var/tmp/sandbox-services/world"


def _client(call: str) -> list[str]:
    return [
        "python3",
        "-c",
        f"import sys; sys.path.append({CLIENT_DIR!r}); import world; print({call})",
    ]


@agent
def world_prober() -> Agent:
    """Calls the service as root, well and badly, and tries it as `nobody`."""

    async def execute(state: AgentState) -> AgentState:
        probes: dict[str, Any] = {}
        listing = await sandbox().exec(["ls", CLIENT_DIR])
        probes["listing"] = listing.stdout.split()
        for name, command in {
            "import": _client("world"),
            "list": ["ls", CLIENT_DIR],
        }.items():
            result = await sandbox().exec(command, user="nobody")
            probes[f"nobody:{name}"] = {
                "success": result.success,
                "stderr": result.stderr,
            }
        for name, call in {
            "ok": "world.call_world('world_report', kind='probe', a=1, b=['x'])",
            "no_kind": "world.call_world('world_report', a=1)",
            "too_big": "world.call_world('world_report', kind='probe', s='a' * 5000)",
        }.items():
            result = await sandbox().exec(_client(call), timeout=60)
            probes[name] = {
                "success": result.success,
                "stdout": result.stdout.strip(),
                "stderr": result.stderr.strip(),
            }
        store().set("probes", probes)
        return state

    return execute


def _run(
    tmp_path: Path,
    *,
    image: str = "python:3.12-slim",
    solvers: list[Solver] | None = None,
    scorer: Scorer | None = None,
) -> EvalLog:
    compose = tmp_path / "compose.yaml"
    compose.write_text(_COMPOSE.format(image=image))
    task = Task(
        dataset=[Sample(input="x")],
        solver=[
            *(solvers if solvers is not None else [world_service()]),
            as_solver(
                swarm(
                    member(world_prober(), name="writer"),
                    controller=collecting(),
                    channels=[],
                )
            ),
        ],
        scorer=scorer,
        sandbox=("docker", str(compose)),
    )
    [log] = eval(
        task, model="mockllm/model", log_dir=str(tmp_path / "logs"), display="none"
    )
    return log


def _events(log: EvalLog, kind: str) -> list[dict[str, Any]]:
    assert log.samples is not None
    [sample] = log.samples
    return [
        dict(e.data)
        for e in sample.events
        if isinstance(e, SwarmEvent) and e.kind == kind
    ]


@pytest.mark.slow
def test_the_service_records_a_root_call_and_refuses_bad_ones(tmp_path: Path) -> None:
    log = _run(tmp_path)
    assert log.status == "success", log.error
    assert log.samples is not None
    [sample] = log.samples
    probes = sample.store["probes"]
    # the client is there when the first member starts
    assert "world.py" in probes["listing"]
    # nobody can neither import it nor list the directory
    assert not probes["nobody:import"]["success"]
    assert "No module named 'world'" in probes["nobody:import"]["stderr"]
    assert not probes["nobody:list"]["success"]
    assert "Permission denied" in probes["nobody:list"]["stderr"]
    # root's good call returns ok; the bad ones get the refusal, with no host path
    assert probes["ok"] == {"success": True, "stdout": "ok", "stderr": ""}
    assert not probes["no_kind"]["success"]
    assert "Error calling method world_report" in probes["no_kind"]["stderr"]
    assert "needs a kind" in probes["no_kind"]["stderr"]
    assert not probes["too_big"]["success"]
    assert "at most 4096 bytes" in probes["too_big"]["stderr"]
    assert "/home/" not in probes["no_kind"]["stderr"] + probes["too_big"]["stderr"]
    # a refusal writes no event of its own; inspect_ai's warning in the transcript
    # is the evidence
    warnings = [
        e.message.message
        for e in sample.events
        if isinstance(e, LoggerEvent) and e.message.level == "warning"
    ]
    assert len(warnings) == 2, warnings
    assert all(
        "Error calling sandbox service method world_report" in w for w in warnings
    )
    assert any("needs a kind" in w for w in warnings)
    assert any("at most 4096 bytes" in w for w in warnings)
    # exactly one report reached the controller and the transcript, from the sandbox
    assert sample.store[SEEN] == [
        {
            "kind": "probe",
            "source": "default",
            "report": {"a": 1, "b": ["x"]},
            "writer": "running",
        }
    ]
    assert _events(log, "world_report") == [
        {
            "version": 1,
            "kind": "probe",
            "source": "default",
            "report": {"a": 1, "b": ["x"]},
        }
    ]
    assert _events(log, "world_service") == [
        {"version": 1, "sandbox": "default", "user": None}
    ]
    assert [(r.kind, r.source) for r in world_reports(sample)] == [("probe", "default")]


@pytest.mark.slow
def test_a_service_without_a_swarm_leaves_no_record(tmp_path: Path) -> None:
    # a chain with world_service() and no swarm(): the report is kept, and the world's
    # events are no evidence of a swarm, so swarm_result() is None live and from the log
    @agent(name="world_sandbox_test_reporter")
    def reporter() -> Agent:
        async def execute(state: AgentState) -> AgentState:
            call = "world.call_world('world_report', kind='alone', up=1)"
            result = await sandbox().exec(_client(call), timeout=60)
            store().set("reported", result.success)
            return state

        return execute

    seen: list[object] = []

    @scorer(metrics=[])
    def reads_the_record() -> Scorer:
        async def score(state: TaskState, target: Target) -> Score:
            seen.append(swarm_result(state))
            return Score(value=1)

        return score

    compose = tmp_path / "compose.yaml"
    compose.write_text(_COMPOSE.format(image="python:3.12-slim"))
    task = Task(
        dataset=[Sample(input="x")],
        solver=[world_service(), as_solver(reporter())],
        scorer=reads_the_record(),
        sandbox=("docker", str(compose)),
    )
    [log] = eval(
        task, model="mockllm/model", log_dir=str(tmp_path / "logs"), display="none"
    )
    assert log.status == "success", log.error
    assert log.samples is not None
    [sample] = log.samples
    assert sample.store["reported"] is True
    assert seen == [None]
    assert swarm_result(sample) is None
    assert [(r.kind, r.source) for r in world_reports(sample)] == [("alone", "default")]


@pytest.mark.slow
def test_a_task_without_the_solver_has_no_service(tmp_path: Path) -> None:
    @agent
    def looker() -> Agent:
        async def execute(state: AgentState) -> AgentState:
            result = await sandbox().exec(["ls", "/var/tmp/sandbox-services"])
            store().set("services", result.stdout.split())
            return state

        return execute

    compose = tmp_path / "compose.yaml"
    compose.write_text(_COMPOSE.format(image="python:3.12-slim"))
    task = Task(
        dataset=[Sample(input="x")],
        solver=swarm(
            member(looker(), name="writer"), controller=collecting(), channels=[]
        ),
        sandbox=("docker", str(compose)),
    )
    [log] = eval(
        task, model="mockllm/model", log_dir=str(tmp_path / "logs"), display="none"
    )
    assert log.status == "success", log.error
    assert log.samples is not None
    assert "world" not in log.samples[0].store["services"]
    assert _events(log, "world_service") == []


@pytest.mark.slow
def test_a_second_service_for_one_sandbox_and_a_sandbox_without_python_are_refused(
    tmp_path: Path,
) -> None:
    log = _run(tmp_path, solvers=[world_service(), world_service(sandbox="default")])
    assert log.status == "error"
    assert log.error is not None
    assert "already has a world service" in log.error.message
    # the first service started; the second never touched the sandbox
    assert len(_events(log, "world_service")) == 1

    (tmp_path / "alpine").mkdir()
    log = _run(tmp_path / "alpine", image="alpine:3.20")
    assert log.status == "error"
    assert log.error is not None
    assert _events(log, "world_service") == []


@pytest.mark.slow
def test_the_service_stops_before_scoring(tmp_path: Path) -> None:
    @scorer(metrics=[])
    def calls_the_service() -> Scorer:
        async def score(state: TaskState, target: Target) -> Score:
            # nothing polls the requests any more: the call never returns
            try:
                await sandbox().exec(
                    _client("world.call_world('world_report', kind='late')"), timeout=3
                )
            except TimeoutError:
                answered = False
            else:
                answered = True
            return Score(
                value=1,
                metadata={
                    "answered": answered,
                    "kinds": [r.kind for r in world_reports(state)],
                },
            )

        return score

    log = _run(tmp_path, scorer=calls_the_service())
    assert log.status == "success", log.error
    assert log.samples is not None
    [sample] = log.samples
    assert sample.scores is not None
    [score] = sample.scores.values()
    assert score.metadata == {"answered": False, "kinds": ["probe"]}


@pytest.mark.slow
def test_under_users_isolation_a_member_cannot_reach_the_client(tmp_path: Path) -> None:
    @agent
    def member_prober() -> Agent:
        async def execute(state: AgentState) -> AgentState:
            # the member's commands run as its own user
            result = await sandbox().exec(_client("world"))
            store().set(
                "as_member", {"success": result.success, "stderr": result.stderr}
            )
            return state

        return execute

    compose = tmp_path / "compose.yaml"
    compose.write_text(_COMPOSE.format(image="python:3.12-slim"))
    task = Task(
        dataset=[Sample(input="x")],
        solver=[
            world_service(),
            as_solver(
                swarm(
                    member(member_prober(), name="writer"),
                    controller=collecting(),
                    channels=[],
                    # no shares: the image has no setfacl, which shares need
                    isolation=users(shared=()),
                )
            ),
        ],
        sandbox=("docker", str(compose)),
    )
    [log] = eval(
        task, model="mockllm/model", log_dir=str(tmp_path / "logs"), display="none"
    )
    assert log.status == "success", log.error
    assert log.samples is not None
    [sample] = log.samples
    assert not sample.store["as_member"]["success"]
    assert "No module named 'world'" in sample.store["as_member"]["stderr"]
    assert sample.store[SEEN] == []
