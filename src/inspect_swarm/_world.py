"""World reports: what the environment's own code says about the environment, for controllers and scorers.

Whatever owns some state outside the members reports a change as it makes it. Host code calls `world_report()`; code in a sandbox calls the same method through the sandbox service that `world_service()` publishes, when the task's solver chain has it before `swarm()`. Each accepted report is written to the transcript as a `SwarmEvent` of kind `world_report` and reaches every swarm running in the sample as a `WorldReport` on `SwarmControl.events()`, before the reporting call returns. `world_reports()` reads the history back from the transcript.
"""

import json
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import cast
from weakref import WeakKeyDictionary

import anyio
from inspect_ai.event import SwarmEvent
from inspect_ai.log import EvalSample, Transcript, transcript
from inspect_ai.log._condense import resolve_events_attachments
from inspect_ai.solver import Generate, Solver, TaskState, solver
from inspect_ai.util import background, sandbox_service
from inspect_ai.util import sandbox as sandbox_env
from pydantic import JsonValue

from ._bus._record import FrozenJson, freeze_json, thaw_json
from ._record import WORLD_REPORT_KIND, WORLD_SERVICE_KIND, swarm_running

MAX_REPORT_BYTES = 4096
"""The most a report may be as JSON, kind and fields together, in UTF-8 bytes."""

MAX_REPORT_DEPTH = 3
"""The deepest a report's fields may nest, counting the report itself as the first level."""

# the source of a report from world_report(); a sandbox's name otherwise
HOST = "host"


@dataclass(frozen=True)
class WorldReport:
    """Something outside the members changed the world and reported it: a snapshot of the state at the change, from host code or from a sandbox the task opted in with `world_service()`."""

    kind: str
    """What the report is about, the reporter's name for the fact, such as `cluster`; a controller dispatches on it."""

    source: str
    """`host` for `world_report()`, else the name of the sandbox the report came from."""

    report: Mapping[str, FrozenJson]
    """The fields the reporter gave, frozen: dicts as mappings, lists as tuples."""


# Who hears a report in each sample: each running swarm registers in run() and removes
# itself in run()'s finally, so a report is before a swarm (transcript only) or during
# it (transcript and its queue). Keyed by the transcript object, as the member names are.
Listener = Callable[[str, str, Mapping[str, FrozenJson]], None]
_listeners: WeakKeyDictionary[Transcript, list[Listener]] = WeakKeyDictionary()

# the sandboxes with a world service in each sample, by the environment's id, so that a
# second service for one sandbox is refused before it resets the first one's directories
_served: WeakKeyDictionary[Transcript, set[int]] = WeakKeyDictionary()


def _listen(listener: Listener) -> Callable[[], None]:  # pyright: ignore[reportUnusedFunction]
    listeners = _listeners.setdefault(transcript(), [])
    listeners.append(listener)

    def remove() -> None:
        listeners.remove(listener)

    return remove


def _check_report(
    kind: object, fields: Mapping[str, JsonValue]
) -> Mapping[str, FrozenJson]:
    # freeze first: freeze_json() refuses a non-string key, a non-JSON value and a
    # container past the depth before visiting inside it, so a cycle or a thousand
    # levels never reach the serializer
    if not isinstance(kind, str) or not kind:
        raise ValueError(
            "A world report needs a kind: a non-empty string naming what it reports."
        )
    try:
        frozen = freeze_json(dict(fields), MAX_REPORT_DEPTH)
    except ValueError:
        raise ValueError(
            f"A world report may nest at most {MAX_REPORT_DEPTH} levels deep, "
            "counting the report itself as the first."
        ) from None
    except TypeError as ex:
        detail = str(ex).split("; ", 1)[-1]
        raise ValueError(f"A world report must be JSON data; {detail}") from None
    try:
        text = json.dumps(
            {"kind": kind, "report": thaw_json(frozen)},
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        )
    except ValueError:
        raise ValueError(
            "A world report must be JSON data; it holds a NaN or an infinity."
        ) from None
    size = len(text.encode())
    if size > MAX_REPORT_BYTES:
        raise ValueError(
            f"A world report may be at most {MAX_REPORT_BYTES} bytes as JSON; "
            f"this one is {size}."
        )
    return cast(Mapping[str, FrozenJson], frozen)


def _world_event(kind: str, data: dict[str, JsonValue]) -> None:
    # as the swarm's other evidence: synchronous, in the span it is written in;
    # swarm_event()'s positional `kind` would collide with the report's own
    transcript()._event(  # pyright: ignore[reportPrivateUsage]
        SwarmEvent(kind=kind, member=None, data={"version": 1, **data})
    )


def _record(kind: str, source: str, frozen: Mapping[str, FrozenJson]) -> None:
    # the event is the record; then every running swarm's queue, with no await between,
    # so the report is in both before the reporting call returns
    _world_event(
        WORLD_REPORT_KIND,
        {"kind": kind, "source": source, "report": thaw_json(frozen)},
    )
    for listener in list(_listeners.get(transcript(), ())):
        listener(kind, source, frozen)


def world_report(kind: str, /, **fields: JsonValue) -> None:
    """Report a change to the world, from host code: a tool as it makes the change, a setup solver, a hook.

    The report is written to the transcript and reaches every swarm running in the sample as a `WorldReport` on `SwarmControl.events()` before this returns. With no swarm running, the transcript keeps it for `world_reports()`. Report the state the change left, as facts the world itself knows (`server="db-2", up=1`), not a verdict on it; the controller holds the rule.

    Args:
        kind: What the report is about, such as `cluster`; a controller dispatches on it.
        **fields: The report, as JSON fields, nested at most `MAX_REPORT_DEPTH` levels and at most `MAX_REPORT_BYTES` as JSON.

    Raises:
        ValueError: If `kind` is empty, a field is not JSON data, or the report is too deep or too large. Nothing is recorded.
    """
    _record(kind, HOST, _check_report(kind, fields))


def world_reports(
    source: TaskState | EvalSample | None = None, kind: str | None = None
) -> list[WorldReport]:
    """Every world report accepted in a sample, in order, from its transcript.

    Args:
        source: Where to read: `None` for the running sample's transcript, as a controller reads it; a scorer's or solver's `TaskState`, for the sample being run or scored; or an `EvalSample` read from a log, for analysis.
        kind: Only the reports of this kind, when given.

    Returns:
        The reports, as the controller received them. A fresh list each call.
    """
    if isinstance(source, EvalSample):
        events = resolve_events_attachments(source.events, source.attachments)
    else:
        events = transcript().events
    reports: list[WorldReport] = []
    for event in events:
        if not isinstance(event, SwarmEvent) or event.kind != WORLD_REPORT_KIND:
            continue
        data = event.data
        if kind is not None and data.get("kind") != kind:
            continue
        reports.append(
            WorldReport(
                str(data["kind"]),
                str(data["source"]),
                cast(
                    Mapping[str, FrozenJson],
                    freeze_json(data["report"], MAX_REPORT_DEPTH),
                ),
            )
        )
    return reports


@solver
def world_service(sandbox: str = "default", user: str | None = None) -> Solver:
    """Let code in a sandbox report changes to the world, through an inspect_ai sandbox service.

    Place it in the task's solver chain before `swarm()`. It publishes the method `world_report` as the sandbox service `world`: code in the container appends `/var/tmp/sandbox-services/world` to `sys.path`, imports `world` and calls `world.call_world("world_report", kind=..., **fields)`, which blocks until the host has recorded the report, with `source` the sandbox's name. The container needs `python3`; a world written in another language runs that client in a `python3 -c` subprocess. The service serves every swarm the chain runs and stops when the solver chain ends, before scoring; scorers read the history with `world_reports(state)`, and a scorer that needs the world's final state reads the world directly.

    Whoever can import the client can report, so root in a shared sandbox can forge a report. Run the world in its own container, or run the members as non-root users with `isolation="users"`.

    Args:
        sandbox: The sandbox to serve, by name; `default` for the sample's default sandbox.
        user: The user the service's files belong to in the sandbox; `None` for the sandbox's default user.
    """

    async def solve(state: TaskState, generate: Generate) -> TaskState:
        if sandbox == HOST:
            raise ValueError(
                f"world_service(): {HOST!r} is the source of host reports; name a sandbox."
            )
        if swarm_running():
            raise RuntimeError(
                "world_service() runs in the task's solver chain, before swarm(); "
                "it cannot start while a swarm runs in the sample."
            )
        env = sandbox_env(sandbox)
        served = _served.setdefault(transcript(), set())
        if id(env) in served:
            raise ValueError(
                f"world_service(): sandbox {sandbox!r} already has a world service "
                "in this sample."
            )
        # returns once the client is written, so a sandbox without python3 or a bad
        # user fails here, in the solver
        handle = await sandbox_service(
            "world",
            {"world_report": report},
            until=lambda: False,
            sandbox=env,
            user=user,
            handle_requests=False,
        )
        assert handle is not None
        served.add(id(env))
        background(_poll, handle, env.default_polling_interval())
        _world_event(WORLD_SERVICE_KIND, {"sandbox": sandbox, "user": user})
        return state

    async def report(kind: str | None = None, **fields: JsonValue) -> str:
        # awaited by inspect_ai; a ValueError reaches the caller as the method's error,
        # a missing kind included
        frozen = _check_report(kind, fields)
        assert isinstance(kind, str)
        _record(kind, sandbox, frozen)
        return "ok"

    return solve


async def _poll(handle: Callable[[], Awaitable[None]], interval: float) -> None:
    # in the sample's task group, so cancelled with the solver chain, before scoring
    while True:
        await anyio.sleep(interval)
        await handle()
