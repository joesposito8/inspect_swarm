"""The world log: records of the environment reach the controller as `WorldChanged` events."""

from contextlib import aclosing
from pathlib import Path
from typing import Any

from inspect_ai import Task, eval  # pyright: ignore[reportUnknownVariableType]
from inspect_ai.agent import react
from inspect_ai.dataset import Sample
from inspect_ai.event import InfoEvent
from inspect_ai.log import EvalSample
from inspect_ai.model import ModelOutput, get_model
from inspect_ai.solver import Generate, Solver, TaskState, solver
from inspect_ai.tool import Tool, tool
from inspect_ai.util import store

from inspect_swarm import (
    Controller,
    StopReason,
    SwarmControl,
    WorldChanged,
    member,
    swarm,
    swarm_controller,
    swarm_record,
    swarm_records,
)

SEEN = "seen"


@tool
def note() -> Tool:
    async def execute(text: str) -> str:
        """Write a note.

        Args:
            text: The note.
        """
        swarm_record(text=text)
        return "noted"

    return execute


@solver
def early_note() -> Solver:
    """A record written before the swarm starts."""

    async def solve(state: TaskState, generate: Generate) -> TaskState:
        swarm_record(text="before")
        return state

    return solve


@swarm_controller
def collecting() -> Controller:
    """Keeps every world record it is handed, with the member states as it saw them."""

    async def run(swarm: SwarmControl) -> StopReason:
        for m in swarm.members:
            swarm.start(m)
        seen: list[dict[str, Any]] = []
        async with aclosing(swarm.events()) as events:
            async for event in events:
                if isinstance(event, WorldChanged):
                    seen.append(
                        {
                            "source": event.source,
                            "record": event.record,
                            "writer": swarm.member("writer").state,
                        }
                    )
        store().set(SEEN, seen)
        return "all_done"

    return Controller(run)


def _call(function: str, **arguments: Any) -> ModelOutput:
    return ModelOutput.for_tool_call("mockllm", function, arguments)


def _run(tmp_path: Path, setup: Solver | None = None) -> EvalSample:
    model = get_model(
        "mockllm/model",
        custom_outputs=[
            _call("note", text="first"),
            _call("note", text="second"),
            _call("submit", answer="done"),
        ],
    )
    task = Task(
        dataset=[Sample(input="Write two notes, then submit.")],
        setup=setup,
        solver=swarm(
            member(react(tools=[note()]), name="writer"),
            controller=collecting(),
            channels=[],
        ),
    )
    [log] = eval(task, model=model, log_dir=str(tmp_path), display="none")
    assert log.status == "success", log.error
    assert log.samples is not None
    [sample] = log.samples
    return sample


def _transcript_records(sample: EvalSample) -> list[Any]:
    return [
        e.data["record"]
        for e in sample.events
        if isinstance(e, InfoEvent)
        and isinstance(e.data, dict)
        and e.data.get("type") == "world"
    ]


def test_records_reach_the_controller_before_the_tool_returns(tmp_path: Path) -> None:
    sample = _run(tmp_path)
    # each record, once, in order, while the member that wrote it is still running:
    # the second note is written right before the member ends, and still comes first
    assert sample.store[SEEN] == [
        {"source": "host", "record": {"text": "first"}, "writer": "running"},
        {"source": "host", "record": {"text": "second"}, "writer": "running"},
    ]
    # the log is readable afterwards, and every record is evidence in the transcript
    assert sample.store["inspect_swarm:world"] == [
        {"text": "first"},
        {"text": "second"},
    ]
    assert _transcript_records(sample) == [{"text": "first"}, {"text": "second"}]


def test_records_written_before_the_swarm_come_first(tmp_path: Path) -> None:
    sample = _run(tmp_path, setup=early_note())
    assert [s["record"]["text"] for s in sample.store[SEEN]] == [
        "before",
        "first",
        "second",
    ]
    assert _transcript_records(sample)[0] == {"text": "before"}


def test_local_sandbox_has_no_file_log(tmp_path: Path) -> None:
    # the host is never given a /swarm: swarm_record() is the whole log there
    model = get_model(
        "mockllm/model",
        custom_outputs=[_call("note", text="local"), _call("submit", answer="done")],
    )
    task = Task(
        dataset=[Sample(input="Write a note, then submit.")],
        solver=swarm(
            member(react(tools=[note()]), name="writer"),
            controller=collecting(),
            channels=[],
        ),
        sandbox="local",
    )
    [log] = eval(task, model=model, log_dir=str(tmp_path), display="none")
    assert log.status == "success", log.error
    assert log.samples is not None
    [sample] = log.samples
    assert [s["record"] for s in sample.store[SEEN]] == [{"text": "local"}]
    assert not Path("/swarm").exists()


def test_swarm_records_outside_a_swarm_is_what_was_written(tmp_path: Path) -> None:
    @solver
    def writes_and_reads() -> Solver:
        async def solve(state: TaskState, generate: Generate) -> TaskState:
            swarm_record(text="alone")
            state.store.set("read", swarm_records())
            return state

        return solve

    [log] = eval(
        Task(dataset=[Sample(input="x")], solver=writes_and_reads()),
        model="mockllm/model",
        log_dir=str(tmp_path),
        display="none",
    )
    assert log.samples is not None
    assert log.samples[0].store["read"] == [{"text": "alone"}]
