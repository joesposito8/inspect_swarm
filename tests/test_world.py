"""World reports: a change to the environment, reported by the code that owns it, reaches the controller and the transcript."""

import json
from contextlib import aclosing
from pathlib import Path
from typing import Any, cast

import anyio
import pytest
from inspect_ai import Task, eval  # pyright: ignore[reportUnknownVariableType]
from inspect_ai.agent import Agent, AgentState, agent, as_solver, react
from inspect_ai.agent import run as run_agent
from inspect_ai.dataset import Sample
from inspect_ai.event import SwarmEvent
from inspect_ai.log import EvalSample, transcript
from inspect_ai.model import ChatMessageUser, ModelOutput, get_model
from inspect_ai.scorer import Score, Scorer, Target, scorer
from inspect_ai.solver import Generate, Solver, TaskState, solver
from inspect_ai.tool import Tool, tool
from inspect_ai.util import background, store

from inspect_swarm import (
    Budget,
    Controller,
    StopReason,
    SwarmControl,
    WorldReport,
    member,
    swarm,
    swarm_controller,
    world_report,
    world_reports,
    world_service,
)
from inspect_swarm._controller._control import SwarmRun
from inspect_swarm._world import (
    MAX_REPORT_BYTES,
    MAX_REPORT_DEPTH,
    _listeners,  # pyright: ignore[reportPrivateUsage]
)

from .swarm_runtime import controller, sample, swarm_events

pytestmark = pytest.mark.anyio

SEEN = "seen"


def _call(function: str, **arguments: Any) -> ModelOutput:
    return ModelOutput.for_tool_call("mockllm", function, arguments)


def _as_dict(report: WorldReport) -> dict[str, Any]:
    return {"kind": report.kind, "source": report.source, "report": dict(report.report)}


def _world_events(sample: EvalSample) -> list[dict[str, Any]]:
    return [
        dict(e.data)
        for e in sample.events
        if isinstance(e, SwarmEvent) and e.kind == "world_report"
    ]


async def _run(swarm_agent: Agent) -> str:
    """Run a swarm in the current sample, as a solver would, and return its stop reason."""
    await run_agent(swarm_agent, [ChatMessageUser(content="task")])
    # the last swarm's final event: a sample may run several
    return str(swarm_events(transcript(), "final")[-1].data["stop_reason"])


@tool
def note() -> Tool:
    async def execute(text: str) -> str:
        """Write a note.

        Args:
            text: The note.
        """
        world_report("note", text=text)
        return "noted"

    return execute


@agent
def noting(texts: list[str]) -> Agent:
    """Reports each text as a note, yielding between them as a tool call would, then ends."""

    async def execute(state: AgentState) -> AgentState:
        for text in texts:
            world_report("note", text=text)
            await anyio.sleep(0)
        return state

    return execute


@swarm_controller
def collecting() -> Controller:
    """Starts every member and keeps every world report it is handed, with the writer's state as it saw it."""

    async def run(swarm: SwarmControl) -> StopReason:
        for m in swarm.members:
            swarm.start(m)
        seen: list[dict[str, Any]] = []
        async with aclosing(swarm.events()) as events:
            async for event in events:
                if isinstance(event, WorldReport):
                    seen.append(
                        {**_as_dict(event), "writer": swarm.member("writer").state}
                    )
        store().set(SEEN, seen)
        return "all_done"

    return Controller(run)


# --- admission: before the reporting call returns ---


class _Holder:
    run: SwarmRun | None = None


async def test_a_report_is_queued_and_recorded_before_the_call_returns() -> None:
    holder = _Holder()
    checked: list[str] = []

    @agent
    def checking_reporter(texts: list[str]) -> Agent:
        async def execute(state: AgentState) -> AgentState:
            for text in texts:
                world_report("note", text=text)
                # no await between the report and these: it is on the queue and in
                # the transcript already, whatever the controller has done with it
                assert holder.run is not None
                queue = holder.run._queue  # pyright: ignore[reportPrivateUsage]
                assert queue[-1] == WorldReport("note", "host", {"text": text})
                assert world_reports(kind="note")[-1].report == {"text": text}
                checked.append(text)
                await anyio.sleep(0)
            return state

        return execute

    async def run(swarm: SwarmControl) -> StopReason:
        assert isinstance(swarm, SwarmRun)
        holder.run = swarm
        for m in swarm.members:
            swarm.start(m)
        async with aclosing(swarm.events()) as events:
            async for _ in events:
                pass
        return "all_done"

    async with sample():
        reason = await _run(
            swarm(
                member(checking_reporter(["first", "second"]), name="writer"),
                controller=controller(run, "holding"),
                channels=[],
            )
        )
        assert reason == "all_done"
        assert checked == ["first", "second"]
        assert [r.report["text"] for r in world_reports()] == ["first", "second"]


# --- reading: live, from a state, from a log ---


async def test_world_reports_outside_a_swarm_is_what_was_reported() -> None:
    async with sample():
        world_report("a", x=1)
        world_report("b", x=2, y=[1, {"z": None}])
        world_report("a", x=3)
        assert [_as_dict(r) for r in world_reports()] == [
            {"kind": "a", "source": "host", "report": {"x": 1}},
            {"kind": "b", "source": "host", "report": {"x": 2, "y": (1, {"z": None})}},
            {"kind": "a", "source": "host", "report": {"x": 3}},
        ]
        assert [r.report["x"] for r in world_reports(kind="a")] == [1, 3]
        assert world_reports(kind="c") == []


def test_a_scorer_and_a_log_read_the_same_reports(tmp_path: Path) -> None:
    @solver
    def early() -> Solver:
        async def solve(state: TaskState, generate: Generate) -> TaskState:
            world_report("setup", ready=True)
            return state

        return solve

    @scorer(metrics=[])
    def reads() -> Scorer:
        async def score(state: TaskState, target: Target) -> Score:
            return Score(
                value=1,
                metadata={"reports": [_as_dict(r) for r in world_reports(state)]},
            )

        return score

    model = get_model(
        "mockllm/model",
        custom_outputs=[_call("note", text="first"), _call("submit", answer="done")],
    )
    task = Task(
        dataset=[Sample(input="Write a note, then submit.")],
        setup=early(),
        solver=swarm(
            member(react(tools=[note()]), name="writer"),
            controller=collecting(),
            channels=[],
        ),
        scorer=reads(),
    )
    [log] = eval(task, model=model, log_dir=str(tmp_path), display="none")
    assert log.status == "success", log.error
    assert log.samples is not None
    [sample] = log.samples
    # the setup report is in the history and reached no controller
    assert [s["kind"] for s in sample.store[SEEN]] == ["note"]
    expected = [
        {"kind": "setup", "source": "host", "report": {"ready": True}},
        {"kind": "note", "source": "host", "report": {"text": "first"}},
    ]
    assert [_as_dict(r) for r in world_reports(sample)] == expected
    assert [_as_dict(r) for r in world_reports(sample, kind="note")] == expected[1:]
    assert sample.scores is not None
    [score] = sample.scores.values()
    assert score.metadata == {"reports": expected}
    # each report is one event, in the caller's span, with the report's kind in data
    assert _world_events(sample) == [
        {"version": 1, "kind": "setup", "source": "host", "report": {"ready": True}},
        {"version": 1, "kind": "note", "source": "host", "report": {"text": "first"}},
    ]
    # the result is read-only
    with pytest.raises(TypeError):
        cast(Any, world_reports(sample)[0].report)["ready"] = False


# --- backlog: which swarms hear a report ---


async def test_reports_outside_a_swarm_reach_no_queue_and_sequential_swarms_hear_their_own() -> (
    None
):
    heard: dict[str, list[str]] = {"first": [], "second": []}

    def run_for(name: str) -> Any:
        async def run(swarm: SwarmControl) -> StopReason:
            for m in swarm.members:
                swarm.start(m)
            async with aclosing(swarm.events()) as events:
                async for event in events:
                    if isinstance(event, WorldReport):
                        heard[name].append(str(event.report["text"]))
            return "all_done"

        return run

    async with sample() as transcript_:
        world_report("note", text="before")
        await _run(
            swarm(
                member(noting(["in first"]), name="writer"),
                controller=controller(run_for("first"), "first"),
                channels=[],
            )
        )
        world_report("note", text="between")
        await _run(
            swarm(
                member(noting(["in second"]), name="writer-2"),
                controller=controller(run_for("second"), "second"),
                channels=[],
            )
        )
        world_report("note", text="after")
        assert heard == {"first": ["in first"], "second": ["in second"]}
        assert [r.report["text"] for r in world_reports()] == [
            "before",
            "in first",
            "between",
            "in second",
            "after",
        ]
        # no listener is left behind
        assert not _listeners.get(transcript_)


async def test_two_swarms_running_at_once_both_hear_a_report() -> None:
    both_started = anyio.Event()
    started = 0
    heard: dict[str, list[str]] = {"a": [], "b": []}

    def run_for(name: str) -> Any:
        async def run(swarm: SwarmControl) -> StopReason:
            nonlocal started
            started += 1
            if started == 2:
                both_started.set()
            for m in swarm.members:
                swarm.start(m)
            async with aclosing(swarm.events()) as events:
                async for event in events:
                    if isinstance(event, WorldReport):
                        heard[name].append(str(event.report["text"]))
            return "all_done"

        return run

    @agent
    def reporter() -> Agent:
        async def execute(state: AgentState) -> AgentState:
            await both_started.wait()
            world_report("note", text="shared")
            return state

        return execute

    @agent
    def brief_waiter() -> Agent:
        async def execute(state: AgentState) -> AgentState:
            await both_started.wait()
            await anyio.sleep(0.05)
            return state

        return execute

    async with sample(), anyio.create_task_group() as tg:
        tg.start_soon(
            _run,
            swarm(
                member(reporter(), name="a-1"),
                controller=controller(run_for("a"), "a"),
                channels=[],
            ),
        )
        tg.start_soon(
            _run,
            swarm(
                member(brief_waiter(), name="b-1"),
                controller=controller(run_for("b"), "b"),
                channels=[],
            ),
        )
    assert heard == {"a": ["shared"], "b": ["shared"]}


def test_a_report_from_background_work_started_before_the_swarm_wakes_the_controller(
    tmp_path: Path,
) -> None:
    @solver
    def late_reporter() -> Solver:
        async def report_later() -> None:
            await anyio.sleep(0.3)
            world_report("world", changed=True)

        async def solve(state: TaskState, generate: Generate) -> TaskState:
            background(report_later)
            return state

        return solve

    @agent
    def napping() -> Agent:
        async def execute(state: AgentState) -> AgentState:
            await anyio.sleep(30)
            return state

        return execute

    @swarm_controller
    def until_changed() -> Controller:
        async def run(swarm: SwarmControl) -> StopReason:
            for m in swarm.members:
                swarm.start(m)
            async with aclosing(swarm.events()) as events:
                async for event in events:
                    if isinstance(event, WorldReport) and event.kind == "world":
                        return "changed"
            return "all_done"

        return Controller(run)

    task = Task(
        dataset=[Sample(input="x")],
        solver=[
            late_reporter(),
            as_solver(
                swarm(
                    member(napping(), name="s"),
                    controller=until_changed(),
                    channels=[],
                    budget=Budget(grace=1),
                )
            ),
        ],
    )
    [log] = eval(task, model="mockllm/model", log_dir=str(tmp_path), display="none")
    assert log.status == "success", log.error
    assert log.samples is not None
    [sample_] = log.samples
    assert [e["kind"] for e in _world_events(sample_)] == ["world"]
    ledger = [
        e for e in sample_.events if isinstance(e, SwarmEvent) and e.kind == "ledger"
    ]
    assert ledger[0].data["stop_reason"] == "controller:changed"


async def test_a_report_in_the_history_stops_the_outage_pattern_before_any_member_starts() -> (
    None
):
    started: list[str] = []

    async def run(swarm: SwarmControl) -> StopReason:
        # the history first, then the members and the stream
        if any(r.report["up"] == 1 for r in world_reports(kind="cluster")):
            return "outage"
        for m in swarm.members:
            swarm.start(m)
            started.append(m.name)
        async with aclosing(swarm.events()) as events:
            async for _ in events:
                pass
        return "all_done"

    async with sample():
        world_report("cluster", up=1)
        reason = await _run(
            swarm(
                member(noting([]), name="writer"),
                controller=controller(run, "outage"),
                channels=[],
            )
        )
    assert reason == "controller:outage"
    assert started == []


async def test_a_report_queued_after_the_iterator_ended_is_yielded_by_the_next_call() -> (
    None
):
    second: list[str] = []

    async def run(swarm: SwarmControl) -> StopReason:
        for m in swarm.members:
            swarm.start(m)
        async with aclosing(swarm.events()) as events:
            async for _ in events:
                pass
        # every member has ended: the stream ended, and a report now waits in the queue
        world_report("note", text="late")
        async with aclosing(swarm.events()) as events:
            async for event in events:
                assert isinstance(event, WorldReport)
                second.append(str(event.report["text"]))
        return "all_done"

    async with sample():
        reason = await _run(
            swarm(
                member(noting(["early"]), name="writer"),
                controller=controller(run, "twice"),
                channels=[],
            )
        )
    assert reason == "all_done"
    assert second == ["late"]


async def test_a_report_during_the_grace_period_is_recorded_and_the_stop_reason_kept() -> (
    None
):
    stopping = anyio.Event()

    @agent
    def held_reporter() -> Agent:
        async def execute(state: AgentState) -> AgentState:
            await stopping.wait()
            world_report("note", text="during the grace period")
            return state

        return execute

    @agent
    def first_reporter() -> Agent:
        async def execute(state: AgentState) -> AgentState:
            world_report("note", text="first")
            await anyio.sleep(30)
            return state

        return execute

    async def run(swarm: SwarmControl) -> StopReason:
        for m in swarm.members:
            swarm.start(m)
        async with aclosing(swarm.events()) as events:
            async for event in events:
                if isinstance(event, WorldReport):
                    stopping.set()
                    return "stop"
        return "all_done"

    async with sample():
        reason = await _run(
            swarm(
                [member(first_reporter(), name="a"), member(held_reporter(), name="b")],
                controller=controller(run, "stopper"),
                channels=[],
                budget=Budget(grace=5),
            )
        )
        assert reason == "controller:stop"
        assert [r.report["text"] for r in world_reports()] == [
            "first",
            "during the grace period",
        ]


async def test_an_error_or_a_cancellation_leaves_no_listener() -> None:
    async def failing(swarm: SwarmControl) -> StopReason:
        raise RuntimeError("the controller's own error")

    @agent
    def napping() -> Agent:
        async def execute(state: AgentState) -> AgentState:
            await anyio.sleep(30)
            return state

        return execute

    async with sample() as transcript_:
        with pytest.raises(RuntimeError, match="the controller's own error"):
            await _run(
                swarm(
                    member(napping(), name="s"),
                    controller=controller(failing, "failing"),
                    channels=[],
                    budget=Budget(grace=0.1),
                )
            )
        assert not _listeners.get(transcript_)
        with anyio.move_on_after(0.3):
            await _run(
                swarm(
                    member(napping(), name="s-2"),
                    controller=collecting(),
                    channels=[],
                    budget=Budget(grace=0.1),
                )
            )
        assert not _listeners.get(transcript_)


# --- ownership ---


async def test_a_report_is_a_snapshot_the_reporter_cannot_change() -> None:
    held: list[WorldReport] = []

    async def run(swarm: SwarmControl) -> StopReason:
        for m in swarm.members:
            swarm.start(m)
        async with aclosing(swarm.events()) as events:
            async for event in events:
                if isinstance(event, WorldReport):
                    held.append(event)
        return "all_done"

    @agent
    def mutating_reporter() -> Agent:
        async def execute(state: AgentState) -> AgentState:
            fields: dict[str, Any] = {"servers": ["db-1"], "up": 1}
            world_report("cluster", **fields)
            fields["servers"].append("db-2")
            fields["up"] = 2
            return state

        return execute

    async with sample():
        await _run(
            swarm(
                member(mutating_reporter(), name="w"),
                controller=controller(run, "holding"),
                channels=[],
            )
        )
        [event] = held
        assert event.report == {"servers": ("db-1",), "up": 1}
        assert world_reports()[0].report == {"servers": ("db-1",), "up": 1}
        with pytest.raises(TypeError):
            cast(Any, event.report)["up"] = 3


# --- validation ---


def _envelope(kind: str, **fields: Any) -> int:
    return len(
        json.dumps(
            {"kind": kind, "report": fields},
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ).encode()
    )


async def test_the_size_cap_counts_the_whole_report_in_bytes() -> None:
    async with sample():
        room = MAX_REPORT_BYTES - _envelope("k", s="")
        world_report("k", s="a" * room)
        assert _envelope("k", s="a" * room) == MAX_REPORT_BYTES
        with pytest.raises(
            ValueError,
            match=f"at most {MAX_REPORT_BYTES} bytes.*{MAX_REPORT_BYTES + 1}",
        ):
            world_report("k", s="a" * (room + 1))
        # a two-byte character counts as two
        with pytest.raises(ValueError, match="bytes"):
            world_report("k", s="a" * (room - 1) + "é")
        with pytest.raises(ValueError, match="bytes"):
            world_report("é" * (MAX_REPORT_BYTES // 2 + 1))
        assert len(world_reports()) == 1


async def test_the_depth_cap_counts_the_report_as_the_first_level() -> None:
    async with sample():
        world_report("k", a={"b": {"c": 1}})
        with pytest.raises(ValueError, match=f"at most {MAX_REPORT_DEPTH} levels"):
            world_report("k", a={"b": {"c": {"d": 1}}})
        deep: dict[str, Any] = {}
        node = deep
        for _ in range(1000):
            node["n"] = {}
            node = node["n"]
        with pytest.raises(ValueError, match="levels"):
            world_report("k", a=deep)
        cycle: dict[str, Any] = {}
        cycle["self"] = cycle
        with pytest.raises(ValueError, match="levels"):
            world_report("k", a=cycle)
        assert len(world_reports()) == 1


async def test_a_report_must_be_json_data_with_a_kind() -> None:
    async with sample():
        for fields in (
            {"a": {1: 2}},
            {"a": {None: 2}},
            {"a": (1, 2)},
            {"a": float("nan")},
            {"a": float("inf")},
            {"a": object()},
            {"a": {"b": b"bytes"}},
        ):
            with pytest.raises(ValueError, match="must be JSON data"):
                world_report("k", **cast(dict[str, Any], fields))
        with pytest.raises(ValueError, match="needs a kind"):
            world_report("")
        with pytest.raises(ValueError, match="needs a kind"):
            world_report(cast(Any, 1), a=1)
        assert world_reports() == []
        # a report may be empty, and `kind` may be a field of its own
        world_report("k")
        world_report("k", kind="inner")
        assert [dict(r.report) for r in world_reports()] == [{}, {"kind": "inner"}]


# --- the service solver's refusals, without a sandbox ---


def _state() -> TaskState:
    return TaskState(
        model=cast(Any, "mockllm/model"), sample_id=0, epoch=0, input="x", messages=[]
    )


async def _generate(state: TaskState, **kwargs: Any) -> TaskState:
    return state


async def test_world_service_refuses_the_host_and_a_running_swarm() -> None:
    async with sample():
        with pytest.raises(ValueError, match="source of host reports"):
            await world_service(sandbox="host")(_state(), cast(Generate, _generate))

    inside: list[BaseException] = []

    async def run(swarm: SwarmControl) -> StopReason:
        try:
            await world_service()(_state(), cast(Generate, _generate))
        except RuntimeError as ex:
            inside.append(ex)
        return "all_done"

    async with sample():
        await _run(
            swarm(
                member(noting([]), name="writer"),
                controller=controller(run, "serving"),
                channels=[],
            )
        )
    [error] = inside
    assert "before swarm()" in str(error)
