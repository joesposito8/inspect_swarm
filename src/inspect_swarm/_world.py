"""The world log: the one record of changes to the environment that reach the controller.

Whatever owns some state outside the members writes a record as it changes that state, checked at the write, so a change that is undone before anyone looks still leaves a line. Host code calls `swarm_record()`; code in a sandbox appends one JSON object per line to `WORLD_LOG` in the sample's default sandbox. The swarm hands each record to the controller as a `WorldChanged` event on `SwarmControl.events()`, at once for `swarm_record()` and within `WORLD_LOG_INTERVAL` seconds for the file, and writes it to the transcript.
"""

import contextvars
from collections.abc import Callable
from typing import Any

from inspect_ai.tool._sandbox_tools_utils.sandbox import (
    resolve_root_access,  # pyright: ignore[reportPrivateUsage]
)
from inspect_ai.util import SandboxEnvironment, store
from inspect_ai.util._sandbox._privileged import (
    privileged_shell,  # pyright: ignore[reportPrivateUsage]
)
from inspect_ai.util._sandbox.context import (
    default_sandbox_environment,  # pyright: ignore[reportPrivateUsage]
    sandbox_default_context_var,  # pyright: ignore[reportPrivateUsage]
)
from inspect_ai.util._sandbox.local import (
    LocalSandboxEnvironment,  # pyright: ignore[reportPrivateUsage]
)
from pydantic import JsonValue

WORLD_DIR = "/swarm"

WORLD_LOG = "/swarm/world.jsonl"
"""The world log's path in the sample's default sandbox: one JSON object per line, appended as the world changes."""

WORLD_LOG_INTERVAL = 1.0
"""Seconds between the swarm's reads of `WORLD_LOG`; a record is seen at most this late, never missed."""

# the host side of the log, in the sample's store
WORLD_KEY = "inspect_swarm:world"

# the run's hook for a record written while it runs, set by SwarmRun
_on_record: contextvars.ContextVar[Callable[[], None] | None] = contextvars.ContextVar(
    "inspect_swarm_on_record", default=None
)


def swarm_record(**fields: JsonValue) -> None:
    """Record a change to the world, from host code: a tool as it makes the change, a setup solver, a hook.

    The record reaches the controller as `WorldChanged("host", fields)` before this returns, when a swarm is running; records written before the swarm starts are delivered first when it does.

    Args:
        **fields: The record, as JSON fields.
    """
    store().set(WORLD_KEY, [*swarm_records(), dict(fields)])
    on_record = _on_record.get()
    if on_record is not None:
        on_record()


def swarm_records() -> list[dict[str, Any]]:
    """Every record host code has written with `swarm_record()` in this sample, in order."""
    return list(store().get(WORLD_KEY, []))


# The file side's setup, once per swarm run, before any member starts: the directory and
# the log exist, belong to the user the swarm runs as (root where it can), and are
# readable by nobody else, so a member running as another user cannot list, read or
# forge them. Paths are positional arguments, never interpolated; every directory above
# the log must be a real, root-owned directory nobody else can write, or the swarm
# refuses, as design/swarm-sandbox-isolation.md's start-of-swarm setup does.
_SETUP = r"""
set -eu
dir="$1"
log="$2"
refuse() { echo "$1" >&2; exit 1; }
parent=$(dirname "$dir")
[ ! -L "$parent" ] && [ -d "$parent" ] || refuse "$parent is not a directory; the world log needs a real directory above it"
owner=$(stat -c %u "$parent")
mode=$(stat -c %a "$parent")
group=${mode%?}; group=${group#"${group%?}"}
other=${mode#"${mode%?}"}
case "$owner:$group$other" in
    0:[01450][01450]) ;;
    *) refuse "$parent is owned by uid $owner with mode $mode; the world log needs every directory above it to be root-owned and not group- or world-writable" ;;
esac
if [ -e "$dir" ] || [ -L "$dir" ]; then
    [ ! -L "$dir" ] && [ -d "$dir" ] || refuse "$dir exists and is not a directory; the world log needs a real directory"
else
    mkdir -m 700 "$dir"
fi
[ "$(realpath "$dir")" = "$dir" ] || refuse "$dir does not resolve to itself; the world log needs a real directory"
if [ -e "$log" ] || [ -L "$log" ]; then
    [ ! -L "$log" ] && [ -f "$log" ] || refuse "$log exists and is not a regular file"
else
    : > "$log"
fi
chmod 700 "$dir"
chmod 600 "$log"
[ "$(id -u)" = 0 ] && chown 0:0 "$dir" "$log"
stat -c '%u %a' "$dir" "$log"
"""


async def setup_world_log(environments: dict[str, SandboxEnvironment]) -> bool:
    """Create the world log in the sample's default sandbox, readable by root only; whether the file side of the log is in use.

    The file side exists only where the swarm can act as root: the `local` sandbox, and a sandbox whose root access is not usable, get none, and `swarm_record()` is the whole log there. The swarm never writes to the host.

    Raises:
        RuntimeError: If the log's path is not safe to use.
    """
    env = default_sandbox_environment(environments)
    try:
        env.as_type(LocalSandboxEnvironment)  # the environments are proxies
    except TypeError:
        pass
    else:
        return False
    if (await resolve_root_access(env)).state != "usable":
        return False
    result = await privileged_shell(
        env, _SETUP, WORLD_DIR, WORLD_LOG, user="root", timeout=60
    )
    if not result.success:
        name = sandbox_default_context_var.get()
        raise RuntimeError(
            f"The world log could not be set up in sandbox {name!r}: {result.stderr.strip() or result.stdout.strip()}"
        )
    return True
