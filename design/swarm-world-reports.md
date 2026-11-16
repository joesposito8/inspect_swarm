# Inspect Swarm: world reports

## Overview

A controller can stop, or wake a member, on the state of the environment the swarm works on, not only on what its members do. Whatever owns some state outside the members reports a change as it makes it: host code calls `world_report(kind, **fields)`, and code in a sandbox calls the same method through an inspect_ai sandbox service that a task opts into by placing the `world_service()` solver before `swarm()`. Each accepted report is a `SwarmEvent` of kind `world_report` in the transcript and a `WorldReport(kind, source, report)` on `SwarmControl.events()`, the fourth kind of `ControlEvent`, for every swarm running in the sample, before the reporting call returns. `world_reports()` reads the history back, live in a controller, from a scorer's `TaskState`, or from a logged sample. A report is a snapshot of the state at the change, never a verdict, so the controller holds the rule. `swarm()` gains no argument.

Code references are to inspect_swarm `main` at `3bad441` and to inspect_ai at the lock's pin, `10dc86e48` (`0.3.290.dev11`). inspect_swarm paths are relative to its repository root, inspect_ai paths to the installed package. Code this design adds is named by file and function, without line numbers. Claims marked *(spike)* were checked by running code under Docker and are not covered by a test.

## Why

`events()` carries what members do: a member ended, went idle or has news. A change in the environment wakes no controller, and a controller woken by other means and then looking at the world misses a change that is undone before it looks. The cluster example shows both. Three ops agents each upgrade one server of a three-server database cluster that needs two servers up, and the controller is to stop the swarm the moment fewer than two are. Two agents taking their servers down at once is visible to a controller woken from a tool wrapper. A restart that takes a server down and straight back up inside one tool call, while another server is down for its upgrade, is not: the controller runs only when the tool's task yields, and by then the cluster is back at quorum. A report made inside the tool, at the change, catches the restart (the restart cases in `tests/test_examples.py`), and the same shape serves a cluster that is a service in its own container, managed by `bash()` members. The store holds a span's net change, so a run with no stop leaves no store trace of the outage, and a bridged member's commands leave no `ToolEvent`: the world's own record is the only record.

## Goals and non-goals

Goals:

- A report written at the change, by the code that owns the state, reaches the controller before the reporting call returns, and is in the transcript whether or not a swarm was running.
- Reports from a sandbox only where the task asks for them, through inspect_ai's `sandbox_service()`, recorded on the host.
- One reader for a controller, a scorer and analysis, over the transcript, returning the type the controller receives.
- A report is bounded in size and nesting and holds JSON facts ([The shape of a report](#the-shape-of-a-report)); a refusal says why without a host path.
- Who can report, and who can forge, is documented ([Security](#security)).
- `swarm()` is unchanged, and controllers that test an event's type before reading its fields need no change.

Non-goals:

- A built-in stop, a new stop reason, or a freeze of actions in flight ([Not this design](#not-this-design)).
- A change to when `events()` ends ([Quiescence](#quiescence)).
- A non-Python client in the container ([Reports from a sandbox](#reports-from-a-sandbox-the-world_service-solver)).
- A drain of the service's pending requests when the solver chain ends ([Reports from a sandbox](#reports-from-a-sandbox-the-world_service-solver)).

## Current behaviour

### What a controller receives

`ControlEvent = MemberEnded | MemberIdle | MemberNews`, three frozen dataclasses with field docstrings (`src/inspect_swarm/_controller/_control.py:208-266`); the union's docstring tells controllers to test an event's type before reading its fields. `events()` (`:786-803`) yields the queue in order and ends when the queue is empty and `_live()` (`:1740-1743`) is false: a member that is `running` or `ending`, or waiting for its verdict, is live; an idle persistent member is not, and is "held until `SwarmControl.wake()`". The queue has four writers, `_publish_idle()` (`:1338`), `_news()` (`:1352`), `_end()` (`:1363`) and `_verified()` (`:1598`): each appends and calls `_notify()` (`:1749`). All four are the runtime's own; nothing outside a member's lifecycle can write it. `wake()` takes only an `idle` member (`:769-780`). The operator's pause, message and stop (`src/inspect_swarm/_operator.py`) act on the runtime without an event.

`Bus.deliver()` binds the sender from the calling member and raises `RuntimeError` outside one (`src/inspect_swarm/_bus/_bus.py:87-103`; `SwarmBus.deliver()` at `:270-276`), so a channel cannot carry a report from host code or from the world either.

### `run()` brackets the swarm per sample

`SwarmRun.run()` sets `current_bus` (`src/inspect_swarm/_controller/_control.py:871`), counts the swarm as running with `running = _swarm_started()` (`:890`), copies the members' context (`:902`), schedules the controller (`:917-919`) and uncounts it in its `finally` with `_swarm_ended(running)` (`:1056`). `src/inspect_swarm/_record.py` keys the count by `id(transcript())` (`:700-713`); `swarm_result()` refuses a read while any swarm in the sample runs, reads the record from the live transcript (`transcript().history.find_last`) or from an `EvalSample`'s events (`:716-760`), and refuses a record kept in the sample store (`:782-785`). `src/inspect_swarm/_evidence.py` keys per-sample state by the `Transcript` object: `_member_names: WeakKeyDictionary[Transcript, set[str]]` (`:441`). `isolation_event()` writes a `SwarmEvent` with `member=None` through `transcript()._event()` (`:56-62`); `swarm_event(kind, *, member, version=1, **data)` (`:40-50`) is the general writer, whose positional `kind` collides with a data key named `kind`.

### `background()` and the sample's task group

inspect_ai's `background()` starts work in the sample's task group (`inspect_ai/util/_background.py`: `sample.tg.start_soon(run)`), after honouring the private `background_target`, which inspect_swarm's `MemberBackground` sets so that work a member starts belongs to the member (`src/inspect_swarm/_background.py`). The runner cancels that task group in the `finally` after the solver plan and before the scorers run (`inspect_ai/_eval/task/run.py`: "ensures that monitor_working_limit() and any coroutines created w/ background() are cancelled"), so background work spans the rest of the solver chain and never scoring; the docstring's "will run as long as the current sample is running" is the longer reading.

### `sandbox_service()`

`sandbox_service(name, methods, until, sandbox, user=None, instance=None, polling_interval=None, started=None, requires_python=True, handle_requests=True)` (`inspect_ai/util/_sandbox/service.py:148-187`). It validates `python3` in the sandbox (`:231`, skipped by `requires_python=False`), resets the request and response directories (`:381-390`) and writes a Python client at `/var/tmp/sandbox-services/<name>/<name>.py` whatever `requires_python` says (`:392-395`); the directory is created mode 700, owned by the service's `user`, under a 1777 parent *(spike)*. Each method is awaited (`SandboxServiceMethod = Callable[..., Awaitable[JsonValue]]`, `:134`; `:600-603`), so a plain function is called and then fails the caller. A request is a JSON file the client writes and the host polls, by default every `SandboxEnvironment.default_polling_interval()` (`inspect_ai/util/_sandbox/environment.py:505`; 0.2 s on Docker); a half-written request is retried on `JSONDecodeError` (`service.py:515`). An exception in a method reaches the caller as `Error calling method <m>: <message>` with the traceback stripped, because host paths in tool output were "observed in 43% of scored samples in one cohort" (`service.py:644`), and is logged as a warning. With `handle_requests=False` the call returns a `safe_handle_requests` coroutine function after `start()` has written the client, for the caller to poll with. External cancellation cancels the service's task group at once; pending requests are not drained (`service.py:276-280`). The generated client's `call_<name>()` polls for its response with no timeout and raises `Exception(response["error"])` on an error (`service.py:869-943`). A round trip from a root caller measured 341 to 506 ms *(spike)*.

### Users isolation, reaping and the store

`setup_isolation()` runs before `run()` (`src/inspect_swarm/_swarm.py:174`); members under `isolation="users"` run as `swarm-<name>`, non-root, and a member's leftover processes are reaped when it ends (`reap_member()`, `MemberReap`). `src/inspect_swarm/_isolation.py` imports `privileged_shell` and `resolve_root_access` from inspect_ai's private modules directly (`:10-12`). Inside the swarm, `suspend_store_events()` makes the swarm's span log one `StoreEvent` with the net change (`inspect_ai/util/_store.py:125-140`); `docs/members.qmd` "Tool state" (`:129-136`) says the store is shared and nothing about net-change logging.

## Design

### At a glance

| | Today | After |
|---|---|---|
| What a controller receives | `MemberEnded`, `MemberIdle`, `MemberNews` | plus `WorldReport(kind, source, report)` |
| Who can write it | the runtime, on a member's lifecycle | any host code, with `world_report(kind, /, **fields)`; code in a sandbox the task opted in with `world_service()` |
| Record | none; the store's net change | a `SwarmEvent` of kind `world_report` per accepted report, in the transcript |
| Reader | none | `world_reports(source=None, kind=None) -> list[WorldReport]`, live or from a log |
| Sandbox side | none | `world_service(sandbox, user)`, a solver before `swarm()`, over `sandbox_service("world", ...)` |
| Bounds | none | a non-empty `kind`, JSON values, depth 3, 4096 bytes serialized |
| `swarm()` | | unchanged |

### Names

```python
# src/inspect_swarm/_world.py; _controller/_control.py imports it for the union below, so the
# two modules do not import each other
@dataclass(frozen=True)
class WorldReport:
    """Something outside the members changed the world and reported it: a snapshot of the state at the change, from host code or from a sandbox the task opted in with `world_service()`."""

    kind: str
    """What the report is about, the reporter's name for the fact, such as `cluster`; a controller dispatches on it."""

    source: str
    """`host` for `world_report()`, else the name of the sandbox the report came from."""

    report: Mapping[str, FrozenJson]
    """The fields the reporter gave, frozen: dicts as mappings, lists as tuples."""


ControlEvent = MemberEnded | MemberIdle | MemberNews | WorldReport
```

```python
# src/inspect_swarm/_world.py, exported from inspect_swarm
def world_report(kind: str, /, **fields: JsonValue) -> None: ...
def world_reports(source: TaskState | EvalSample | None = None, kind: str | None = None) -> list[WorldReport]: ...
@solver
def world_service(sandbox: str = "default", user: str | None = None) -> Solver: ...
```

The names are Ransom's (decision: Ransom, 2026-11-03). `kind` is positional-only, so a report may have a field named `kind`. `HOST = "host"` is the source of a host report. `FrozenJson`, `freeze_json()` and `thaw_json()` are `src/inspect_swarm/_bus/_record.py`'s (`:9`, `:108`, `:133`), the bus's own for a frozen record.

### The shape of a report

`kind` is required and non-empty (decision: Joey, 2026-11-13): it names the fact, so a controller dispatches on `event.kind` and a scorer filters in one call. The body is a free JSON object. The reporter supplies `kind` and the fields; the swarm supplies `source`, the order and the transcript's timestamp and span.

A report is a snapshot of the world at the change, taken in the code that owns the state, and the controller holds the rule (decision: Joey, 2026-11-13). From `examples/stop_on_outage.py`:

```python
# change(), the cluster's one write path, as it changes the state
world_report(KIND, action=action, server=server, up=up)

# until_outage(), the controller
if isinstance(event, WorldReport) and event.kind == KIND and below(event.report, quorum):
    return reason

# cluster_upgraded(), the scorer: its verdict is the final state; the reports are its metadata
reports = [dict(r.report) for r in world_reports(state, kind=KIND)]
```

`up=1`, never `outage=True`: a field true only under one experiment's rule belongs to the controller, and one world then serves every rule; a count, a sum or a version the world alone computes is a fact. Two more conditions: a snapshot, not a delta (`up=1`, not `change="down"`), because two sandbox requests may be handled out of write order; and taken under the world's lock and sent after it, because `call_world()` blocks for the round trip. The library adds no identity field: a host report lands in its tool's span, which names the member, and a world may name who asked, as the service example's `by` does, which is the reporter's claim ([Security](#security)).

### Validation

`_check_report(kind, fields) -> Mapping[str, FrozenJson]` refuses in this order, each with a `ValueError` that carries no host path, raised before the transcript or any listener changes:

1. `kind` not a non-empty `str`: "A world report needs a kind: a non-empty string naming what it reports."
2. `freeze_json(dict(fields), MAX_REPORT_DEPTH)` refuses a container past the depth before visiting inside it, so a thousand-level input or a cycle never reaches `RecursionError`: "A world report may nest at most 3 levels deep, counting the report itself as the first." A non-string key, a tuple or any non-JSON type: "A world report must be JSON data; <freeze_json's detail>". Both errors are re-raised as `ValueError` `from None`.
3. `json.dumps({"kind": kind, "report": thaw_json(frozen)}, ensure_ascii=False, allow_nan=False, separators=(",", ":"))` refuses `NaN` and infinities: "A world report must be JSON data; it holds a NaN or an infinity." Its UTF-8 length over `MAX_REPORT_BYTES`, multibyte characters and the envelope counted: "A world report may be at most 4096 bytes as JSON; this one is <n>."

The caps are `MAX_REPORT_BYTES = 4096` and `MAX_REPORT_DEPTH = 3`, the report object being level 1 (decision: Ransom, 2026-11-16): a report is a small snapshot, not a dump, and both are module constants a world may raise. Both entrypoints pass the frozen mapping, never the raw fields, to the record.

### Recording

`_record(kind, source, frozen)` runs synchronously, with no `await` between its two steps: it writes the evidence event, which is the record, then calls every listener registered for the sample's transcript with `frozen`. The transcript is the history, as it is for the swarm's result record ([swarm-result-record.md](swarm-result-record.md)); nothing goes to the store, so store-event suppression is not a concern. The guarantee is "queued before the call returns", never "the controller has processed it". `world_report()` is `_record(kind, HOST, _check_report(kind, fields))`.

```python
# src/inspect_swarm/_world.py
def _world_event(kind: str, data: dict[str, JsonValue]) -> None:
    # as isolation_event(): swarm_event()'s positional `kind` would collide with the report's
    transcript()._event(SwarmEvent(kind=kind, member=None, data={"version": 1, **data}))


def _record(kind: str, source: str, frozen: Mapping[str, FrozenJson]) -> None:
    _world_event("world_report", {"kind": kind, "source": source, "report": thaw_json(frozen)})
    for listener in list(_listeners.get(transcript(), ())):
        listener(kind, source, frozen)
```

`WorldReport.report` is the frozen mapping, read-only as a bus `Record`'s data is: a world that mutates its dict after reporting, or a controller that mutates an event, changes nothing another reader holds.

### Delivery: a per-sample registry

```python
# src/inspect_swarm/_world.py
Listener = Callable[[str, str, Mapping[str, FrozenJson]], None]
_listeners: WeakKeyDictionary[Transcript, list[Listener]] = WeakKeyDictionary()

def _listen(listener: Listener) -> Callable[[], None]:
    # registers for the current sample; returns the remover
```

```python
# src/inspect_swarm/_controller/_control.py, SwarmRun
def _report(self, kind: str, source: str, report: Mapping[str, FrozenJson]) -> None:
    self._queue.append(WorldReport(kind, source, report))
    self._notify()

# in run(): beside running = _swarm_started()
unlisten = _listen(self._report)
# in the same finally as _swarm_ended(running)
unlisten()
```

The registry is keyed by the `Transcript` object, as `_evidence.py` keys `_member_names`, never by `id()`. `run()` registers after `_swarm_started()`, before any member or the controller starts, and removes in the `finally` that already uncounts the swarm, so an error or a cancellation anywhere in the run removes it (decision: Joey, 2026-11-15). Every swarm running in the sample hears every report; with none running, the transcript alone keeps it. The service's reports arrive from a `background()` task started before the swarm, which a context variable set in `run()` would never reach ([Alternatives considered](#alternatives-considered)).

**Backlog: no replay.** A queue holds only the reports accepted while its swarm is registered; earlier reports are in the transcript, and a controller that needs the world as it already stands reads `world_reports()` first, as `until_outage()` does. Registration and `_record()` share one event loop, so a report is before registration (transcript only) or after (transcript and queue); sequential swarms hear only their own, and concurrent swarms both hear what is accepted while both are registered.

**History and events overlap.** Registration precedes the controller's scheduling, so a report accepted between them is both in `world_reports()` and on the queue; the two are not an exactly-once stream, which suits idempotent decisions such as the outage rule. A controller that wakes members on reports reads live events only and checks `swarm.member(name).state == "idle"` before `wake()`, as `lead_and_workers` does for `MemberNews`.

### Reports from a sandbox: the `world_service()` solver

Opt-in is the solver's presence in the task's chain before `swarm()` (decision: Ransom, 2026-11-03 for opt-in and `sandbox_service()`; decision: Joey, 2026-11-15 for the solver as the vehicle), as `solver=[world_service(sandbox="cluster"), as_solver(swarm(...))]`; with no `world_service()` nothing in the library touches a sandbox.

```python
# src/inspect_swarm/_world.py
@solver
def world_service(sandbox: str = "default", user: str | None = None) -> Solver:
    async def solve(state: TaskState, generate: Generate) -> TaskState:
        if sandbox == HOST:
            raise ValueError(f"world_service(): {HOST!r} is the source of host reports; name a sandbox.")
        if swarm_running():  # inside a member, background() is the member's
            raise RuntimeError("world_service() runs in the task's solver chain, before swarm(); "
                               "it cannot start while a swarm runs in the sample.")
        env = sandbox_env(sandbox)
        served = _served.setdefault(transcript(), set())
        if id(env) in served:  # a second sandbox_service("world") would reset the first's directories
            raise ValueError(f"world_service(): sandbox {sandbox!r} already has a world service in this sample.")
        # returns once the client is written, so no python3 or a bad user fails here
        handle = await sandbox_service(
            "world", {"world_report": report}, until=lambda: False,
            sandbox=env, user=user, handle_requests=False,
        )
        served.add(id(env))
        background(_poll, handle, env.default_polling_interval())  # sleep, handle(), forever
        _world_event("world_service", {"sandbox": sandbox, "user": user})
        return state

    async def report(kind: str | None = None, **fields: JsonValue) -> str:
        # kind defaults to None so a call without one gets _check_report()'s ValueError
        # as the method's error, not a TypeError from binding the call
        frozen = _check_report(kind, fields)
        assert isinstance(kind, str)
        _record(kind, sandbox, frozen)
        return "ok"

    return solve
```

- `report` is `async` because inspect_ai awaits every method. A `ValueError` reaches the caller as `Error calling method world_report: <message>` and records nothing ([Evidence](#evidence)). `source` is the sandbox's name as given, `default` for the default sandbox.
- The container must have `python3`, and `requires_python` is left on (decision: Ransom, 2026-11-16); a world in another language runs the client in a subprocess: `python3 -c "import sys; sys.path.append('/var/tmp/sandbox-services/world'); import world; world.call_world('world_report', kind='cluster', server='db-2', up=1)"`.

**Lifetime.** The poller lives in the sample's task group, which the runner cancels after the solver plan and before the scorers run. The service therefore spans the rest of the solver chain, later solvers and sequential swarms included, and stops before final scoring. Scorers read the accepted history through `world_reports(state)` and cannot expect the RPC to be live; a host `world_report()` from a scorer still records, with no wake. Pending requests are not drained when the chain ends, and nothing is asked of inspect_ai for it (decision: Ransom, 2026-11-16): a scorer that needs the world's final state reads the world directly, through the sandbox or the store.

**Acceptance boundary.** A report is accepted when `_record()` has run. The transcript holds the history of accepted reports and no more: a request unread when the poller is cancelled is lost, one in flight may be lost, and the client call that wrote it blocks without timeout until its container is torn down. A report accepted in the swarm's last instant loses only its wake. The service example sends on the request's thread, snapshots under the lock and reports after it, in order, before it replies ([The shape of a report](#the-shape-of-a-report)); a background thread for sending could let the action finish and the sample close before the report arrives.

The client, in the container:

```python
import sys
sys.path.append("/var/tmp/sandbox-services/world")
import world
world.call_world("world_report", kind="cluster", server="db-2", up=1)   # blocks for the round trip
```

### Quiescence

`events()` ends when the queue is empty and no member is live, as today (decision: Joey, 2026-11-15; decision: Ransom, 2026-11-16). A report accepted after the iterator ended waits in the queue for the next `events()` call, so a controller that waits on a quiet world with idle members sleeps and calls `events()` again, and misses nothing. Keeping the stream open while a service runs and a member is idle would not do: an idle persistent member never ends on its own, so an all-idle swarm with a quiet world would block until a cap instead of returning, and the runtime does not know whether a reporter exists. An opt-in flag on `events()` is built when someone needs it.

### Evidence

Two kinds of `SwarmEvent`, both with `member=None`, written through `_world_event()`:

| `kind` | Written | `data` besides `version` |
|---|---|---|
| `world_report` | once per accepted report, in the caller's span for a host report and in the `world_service()` solver's span for a sandbox report | `kind` (the report's), `source`, `report` |
| `world_service` | once per `world_service()` solver, after its service has started, in the solver's span | `sandbox`, `user` |

The viewer's header shows the outer kind; the report's kind is in `data`. A sandbox report's event lands in the `world_service()` solver's span because the poller is a `background()` task started there, and the transcript places an event in the span that was current when its task started *(spike)*; so every sandbox report of a sample sits under its solver, in order, apart from the members' spans.

A refused report writes no event (decision: Ransom, 2026-11-16). The caller gets the `ValueError`'s text, and for a sandbox report inspect_ai also logs a warning, `Error calling sandbox service method world_report: <message>`, which the transcript records as a `LoggerEvent`; the Docker test checks for it ([Testing](#testing)).

### Reading reports

`world_reports(source=None, kind=None)` scans the `SwarmEvent`s of kind `world_report`: `transcript().events` when `source` is `None` (a controller has no `TaskState`) or a scorer's `TaskState`, and an `EvalSample`'s events with their attachments resolved first (`resolve_events_attachments`, as `swarm_result()` reads a logged sample). With `kind` it returns only that kind. Each is rebuilt as `WorldReport(kind, source, freeze_json(report, MAX_REPORT_DEPTH))`, so a controller, a scorer and an analysis script hold one type and equal reports. The result is a fresh list each call; each report's mapping is frozen and read-only.

### Examples

The cluster upgrade, as `artifact` tasks (`result=artifact_result()`: the result is the environment, and "Scoring what members leave" says to run every member to its end, so the task parameter `stop="all_done"` swaps in `leaderless(stop="end")`). One example per path, sized like the repo's others (decision: Joey, 2026-11-16):

- `examples/stop_on_outage.py`, one file, no sandbox: the cluster in the sample store, with `change()` its one write path, which saves the state and calls `world_report("cluster", action=..., server=..., up=...)`; `restart()` makes two changes with nothing yielding between them, so only the reports show the instant below quorum; `until_outage(quorum=2)` reads `world_reports(kind="cluster")` first, then stops at the first `WorldReport` with `up` below its quorum; `cluster_upgraded()` scores `CORRECT` when every server is up at v2 and puts the final state and every cluster report in its metadata; three `react()` members with the cluster tools and `swarm_tools()`.
- `examples/stop_on_outage_service.py` with `cluster_service.py` and `stop_on_outage_service.yaml` (Docker): a standard-library `ThreadingHTTPServer` in the `cluster` container takes each snapshot (`action`, `server`, `by`, `up`) under its lock and sends the reports after releasing it, in order, through the `world` client; the chain is `[world_service(sandbox="cluster"), as_solver(swarm(...))]`; the controller is `until_outage()` imported from `stop_on_outage.py`, unchanged; three `react()` members with `bash()` in their own containers reach the service only with `curl` over an internal network; `cluster_upgraded_in()` reads the service's state file for the verdict and `world_reports(state, kind="cluster")` for the metadata.

### Docs

`docs/controllers.qmd`: the `events()` bullet names `WorldReport`; a section of its own, "Reports from the world", after Persistent members: what a report is and its caps, the `world_reports()`-first idiom with `until_outage` from the example, the quiescence rule and the sleep-and-call-again workaround, `world_service()` in the chain with the client snippet and the `python3 -c` one-liner, what a refused report leaves, forgery and the two layouts, facts only, the service's end before scoring and reading the world for its final state. `docs/analysis.qmd`: a "World reports" subsection on the two event kinds, the outer kind against `data["kind"]`, and `world_reports(sample, kind=...)`. `docs/members.qmd`, "Tool state": store changes inside the swarm are logged as the net change, so a world tool returns the state it leaves or reports it. `docs/reference/inspect_swarm.qmd`: `WorldReport` beside the other events and a "World reports" group with the three functions. `examples/README.md`: the two rows and the Docker note.

## Alternatives considered

**A file the swarm polls**: a root-only `/swarm/world.jsonl` in the default sandbox, created before any member starts, read whole on an interval. Every swarm with usable root creates it; a visible path; a 1 s poll; root setup and symlink checks in the runtime. The service instead (decision: Ransom, 2026-11-03).

**A `reports=` keyword on `swarm()`**, or a `ReportService` object the swarm starts and stops: eight pieces for one door, and the service's lifetime would be one swarm's, where a chain may run several. The solver's lifetime is the chain's ([Lifetime](#reports-from-a-sandbox-the-world_service-solver)), its settings are logged through the solver's recorded arguments and its `world_service` event, and several worlds are several solvers. Rejected.

**A channel.** `Bus.deliver()` binds the sender from the calling member and raises outside one ([What a controller receives](#what-a-controller-receives)); a report has no member sender. Rejected.

**A context-variable hook** that the runtime sets in `run()` for `world_report()` to call: a `background()` task started before the swarm never inherits it, so the service's reports would reach no controller. The registry keyed by the transcript does. Rejected.

**Seeding the queue** with the reports written before the swarm started: re-delivers what the history already holds, where `world_reports()` gives the controller the state in one call. Rejected.

**A shell client** for non-Python worlds: `requires_python=False` only skips the `python3` check and the Python client is written regardless; the request protocol (`requests/<uuid>.json` with `id`, `method`, `params`; `responses/<uuid>.json` with `id`, `result`, `error`; half-written requests retried) is inspect_ai's private layout, and shell cannot emit arbitrary JSON without `jq` or Python. Rejected ([Reports from a sandbox](#reports-from-a-sandbox-the-world_service-solver)).

**A store list of reports** that `world_reports()` reads back: the transcript is the record, as `swarm_result()` made it for the result record, and `_record.py` refuses a record kept in the sample store. Rejected.

**Keeping `events()` open while a service runs** ([Quiescence](#quiescence)). Rejected.

**A timer in the controller** polling the world: a poll's miss is the miss in [Why](#why). Rejected.

**A member calling into the runtime from its tools**: covers only native members whose tools are wrapped, and misses a change undone inside one call. Rejected.

## Compatibility and migration

- **Controllers.** `ControlEvent` gains a kind without `member`. A controller that tests an event's type before reading its fields, as the union's docstring asks, needs no change. In this repository, 20 reads of `event.member` on the bare union, in 17 functions across six test files, `examples/lead_and_workers.py` and its copy in `docs/controllers.qmd`, gain an `isinstance` guard in the code PR.
- **Public API.** New exports: `WorldReport`, `world_report`, `world_reports`, `world_service`. `swarm()` is unchanged. A `feat:` change, not breaking.
- **Eval logs.** New logs may hold `SwarmEvent`s of kinds `world_report` and `world_service`; readers that switch on `kind` ignore them. Nothing in the store ([Recording](#recording)).
- **inspect_ai.** No change. The design uses `sandbox_service()`, `background()`, `sandbox()`, `SandboxEnvironment.default_polling_interval()`, `transcript()`, `TaskState`, `EvalSample` and `SwarmEvent` (public), and `transcript()._event()` and `inspect_ai.log._condense.resolve_events_attachments` (private, as the swarm's other evidence and `swarm_result()` do).

## Security

- **Who can report.** Any host code in the sample: a tool, a solver, a hook, a scorer. In a sandbox, any process that can import the client: the directory is mode 700 and owned by the service's user, under a 1777 parent whose listing shows the service's name, so root in a shared sandbox can forge a report. The world in its own container is the recommended layout, and the service example's; a world that shares the members' container should run with `isolation="users"`, under which members run as `swarm-<name>`, non-root, and `nobody` gets `No module named 'world'` and `Permission denied` ([Testing](#testing)). The world's process must not be a member's child, since reaping kills it. `world_service()` runs before `setup_isolation()` creates the users, which changes nothing: the directory is the service user's, mode 700.
- **What a report holds.** Facts about the world, as JSON, under the caps ([The shape of a report](#the-shape-of-a-report)). Never agent text (decision: Ransom, 2026-11-03): a report that carries a member's words is a channel outside the bus, with no fencing and no evidence. The docs say so.
- **Refusals.** A refusal names the cap or the offending key, never a host path; inspect_ai strips tracebacks from service errors for the same reason.
- **Eval awareness.** Host reports add nothing visible in any container. A world in its own container adds nothing in the members' container. A world in the members' container adds a `world` directory under the 1777 `/var/tmp/sandbox-services`, a root `find` every 0.2 s, and a readable client if a member is root: smaller than the `inspect-task-*` hostnames and `swarm-<member>` users already present. A world that reports inside its request handler answers slower by the round trip (decision: Joey, 2026-11-15, that this is stated).

## Testing

All tests use mockllm and stub agents, run on asyncio and trio through `tests/conftest.py`, and need no network, Docker or model provider, except those marked Docker, which are `slow` and run with `pytest --runslow`.

- **Admission** (`tests/test_world.py`): inside the reporting agent, with no `await` after `world_report()`, the swarm's queue and `world_reports(kind=...)` both hold the report, twice in turn.
- **Reading** (`tests/test_world.py`): outside any swarm, three reports read back in order with `source="host"` and lists as tuples, `kind=` selecting one kind and an unknown kind giving `[]`; in an `eval()`, a setup solver's and a tool's reports are the same list from the scorer's `TaskState` and from the logged `EvalSample`, the setup report reaches no controller, each report is one `world_report` event with `data` `version`, `kind`, `source` and `report`, and a mapping read back refuses assignment.
- **Backlog** (`tests/test_world.py`): reports before, between and after two sequential swarms are in the history and on no queue, each swarm hearing only its own; two concurrent swarms with distinct member names both hear one report; no listener remains afterwards; a report from a `background()` task started before the swarm wakes the controller, which stops `controller:changed`; a report already in the history stops the outage pattern before any member starts.
- **Quiescence and the stop** (`tests/test_world.py`): a report made after `events()` ended, every member having ended, is yielded by the next `events()` call; a report during the grace period is recorded and the first stop reason kept; the registry is empty after a controller error and after an outer cancellation with a short `Budget.grace`.
- **Ownership** (`tests/test_world.py`): the reporter mutates its dict after reporting and the event and `world_reports()` hold the snapshot; the event's mapping refuses assignment.
- **Validation** (`tests/test_world.py`): `MAX_REPORT_BYTES` exactly accepted and one byte more refused, naming the cap and the size; a two-byte character counts as two, in a field and in `kind`; depth 3 accepted, 4 refused; a thousand-level input and a cycle refused as depth without `RecursionError`; an integer key, a `None` key, a tuple, `NaN`, `inf`, an `object()` and `bytes` refused as not JSON; an empty and a non-string `kind` refused; nothing recorded after any refusal; an empty report and a field named `kind` accepted.
- **The solver's refusals** (`tests/test_world.py`): `world_service(sandbox="host")` raises `ValueError`; `world_service()` inside a running swarm raises `RuntimeError`.
- **The service** (`tests/test_world_sandbox.py`, Docker, `python:3.12-slim`): the client is present when the first member starts; `nobody` gets `No module named 'world'` importing it and `Permission denied` listing it; a root call returns `ok` and is the one report in the queue and the transcript, `source="default"`; a call without `kind` and an oversize call fail with `Error calling method world_report` and the refusal text, no host path, no event, and two `LoggerEvent` warnings `Error calling sandbox service method world_report`; one `world_service` event with `sandbox="default"`, `user=None`; a task without the solver has no `world` directory and no event; a second `world_service()` for the same sandbox fails the eval with `already has a world service` after one event; `alpine`, without `python3`, fails it with none; a scorer's client call times out while `world_reports(state)` holds the earlier report; under `users(shared=())` a member's `sandbox().exec` of the client gets `No module named 'world'` and the controller hears nothing.
- **Examples** (`tests/test_examples.py`): `stop_on_outage.py`: members taking turns end `all_done`, score `C` and leave nine reports, none below quorum; two taking their servers down at once stop `controller:outage` during their upgrades, only `db-3` up, scored `I`; a restart while another server is down stops on the `restart:down` report, the `restart` report next, the restart's `ToolEvent` already showing the server up; every member taking its server down and submitting at once stops on the outage, the scorer's reports equal to `world_reports(sample)`. `stop_on_outage_service.py` (Docker): the same two stops, every report `source="cluster"`, the outage report's `by` naming the member's container, the restart's reports with `up` 1 then 2.

## Implementation plan

One pull request, `feat: world reports, so a controller can stop on the state of the environment`:

1. **World reports.**
   - `src/inspect_swarm/_world.py` (new): `WorldReport`; `MAX_REPORT_BYTES`, `MAX_REPORT_DEPTH`, `HOST`; `Listener`, `_listeners`, `_listen()`, `_served`; `_check_report()`, `_world_event()`, `_record()`, `_poll()`; `world_report()`, `world_reports()`, `world_service()`.
   - `src/inspect_swarm/_controller/_control.py`: `WorldReport` in `ControlEvent`; `SwarmRun._report()`; `_listen(self._report)` in `run()` after `_swarm_started()`, `unlisten()` in the `finally` before `_swarm_ended()`.
   - `src/inspect_swarm/_record.py`: `swarm_running()`. `src/inspect_swarm/__init__.py`: the four exports. `src/inspect_swarm/_swarm.py`: unchanged.
   - `examples/stop_on_outage.py`, `examples/stop_on_outage_service.py`, `examples/cluster_service.py`, `examples/stop_on_outage_service.yaml` (new); `examples/README.md`: two rows and the Docker note; `examples/lead_and_workers.py`: `isinstance(event, MemberNews | MemberIdle)` before `event.member`.
   - `docs/controllers.qmd`: the `events()` bullet, "Reports from the world", the `lead_and_workers` copy's guard; `docs/analysis.qmd`: "World reports"; `docs/members.qmd`: the net-change bullet under Tool state; `docs/reference/inspect_swarm.qmd`: `WorldReport` and a "World reports" group.
   - Tests: `tests/test_world.py` and `tests/test_world_sandbox.py` (new); the outage cases in `tests/test_examples.py`; `isinstance` guards at 20 reads of `event.member` in 17 functions of `tests/test_controller.py`, `tests/test_final.py`, `tests/test_metrics.py`, `tests/test_persistent.py`, `tests/test_result.py` and `tests/test_turn_seam.py`.
   - Depends on: nothing. The edits in [Changes to other designs](#changes-to-other-designs) ship with this document.

## Changes to other designs

- [swarm-api.md](swarm-api.md): `WorldReport` in the `events()` comment and in the `ControlEvent` alias; the `events()` bullet's closing sentence, which named quiescence as the kind without `member`, now says that `ControlEvent` is a union, that `MemberIdle` and `MemberNews` must not be mistaken for an ending, and that `WorldReport` has no `member`.
- [swarm-event-types.md](swarm-event-types.md): `WorldReport` in the `ControlEvent` alias; the rows `world_report` and `world_service` in the table of the swarm's other evidence, the latter in the solver's span; `_world_event()` among the writers of that evidence; `world_reports()` under "How inspect_swarm reads them", matching the `SwarmEvent`s of kind `world_report` live or from an `EvalSample` with its attachments resolved.
- [swarm-implementation-plan.md](swarm-implementation-plan.md): a world reports line under Future work.

## Open questions

None. The five this design raised are decided:

1. **The caps** are 4096 bytes and depth 3, module constants a world may raise (decision: Ransom, 2026-11-16; [Validation](#validation)).
2. **Non-Python worlds**: the container has `python3`, `requires_python` is left on, and a world in another language runs the client in a subprocess (decision: Ransom, 2026-11-16; [Reports from a sandbox](#reports-from-a-sandbox-the-world_service-solver)).
3. **A refused report** writes no event; the caller's error and inspect_ai's warning are the evidence (decision: Ransom, 2026-11-16; [Evidence](#evidence)).
4. **`events()` while every member is idle** keeps its rule; a controller sleeps and calls it again, and an opt-in flag is built when someone needs it (decision: Ransom, 2026-11-16; [Quiescence](#quiescence)).
5. **Pending requests when the chain ends** are not drained and nothing is asked of inspect_ai; a scorer that needs the final state reads the world (decision: Ransom, 2026-11-16; [Reports from a sandbox](#reports-from-a-sandbox-the-world_service-solver)).

## Not this design

- **A built-in stop, or a new stop reason.** The controller returns its own reason, as `until_proved()` does.
- **A freeze of actions in flight.** A tool call or sandbox command dispatched before the stop still finishes, as today ([swarm-limits.md](swarm-limits.md)); a stop that ends a bridged member's running `exec` is a separate design.
- **`filesystem_audit()`** records who wrote which file; a world report records what the world's own code found. They meet where a world is files.
- **Members reporting.** A member's tool may call `world_report()` as host code, and the report lands in its span; a report as a member's channel to the controller is the bus's job.
