# Inspect Swarm: members that choose their own names

Status: proposed, 2026-10-22. Issue: [#61](https://github.com/meridianlabs-ai/inspect_swarm/issues/61). Author: agent (Claude), reviewed by Codex; see the PR.

## Overview

A new member option, `member(..., choose_name=True)`, leaves the swarm's name for the member out of everything the swarm tells it. Its preamble says how many agents are in the swarm and asks it to choose a name for itself and tell the others what it is, instead of opening with "You are react-1". Its role, if it has one, is still stated. The swarm keeps its own name for the member for everything it tracks (timelines, spans, tool-state scopes, budget shares, the ledger and the result record), so the option changes only what the agent is told.

The `filesystem` channel, the default, keeps working: instead of naming the member's scratch directory after the swarm's name, it tells the member to keep its work in a directory named with the name it chose. Channels whose tools show members to each other by the swarm's names (the shared-memory channel and its `swarm_wait`) cannot be combined with the option, and `swarm()` refuses the combination with an error, as it does for any channel whose instructions would reveal the name. The result record says which members chose their own names; the name a member chose is in its transcript, not in the swarm's records.

## Why

Every member's preamble opens with the name the swarm assigned, `You are {name}, one of {N} agents in a swarm…` (`src/inspect_swarm/_swarm.py:139-143`). The name comes from `member(name=...)` or the agent's registry name, numbered when several members share it (`react-1` to `react-4`), so a member always starts with an identity the eval author or the framework handed out, and a default name also tells the model which agent framework it runs in. An eval that studies how agents handle identity and coordination when nobody assigns them a name (do they pick names, do two pick the same one, how do they find out who is who) has no way to run that condition today (issue #61).

## Goals and non-goals

Goals:

- A per-member option that keeps the swarm's name for the member out of everything the swarm shows that member: the preamble and the built-in channels' instructions.
- The swarm's internal name, and everything keyed on it, unchanged.
- Each built-in channel either works without revealing names or is refused with a clear error; a user channel whose instructions would reveal the name is refused too.
- The result record says which members chose their own names, so analysis can tell the conditions apart per member.

Non-goals:

- Registering the name a member chose with the swarm, so that channels could show chosen names and the record could store them. See [Alternatives considered](#alternatives-considered) and [Not this design](#not-this-design).
- Hiding names that the eval author's own code or configuration shows the agent: the sample input, a controller's `start(input=...)`, the member's agent and its prompt, or the sample's sandbox configuration.
- Making the shared-memory channel, `swarm_wait` or the later messages channel work with members that choose their names.

## Current behaviour

Code references are to inspect_swarm `main` at `80a18d6`.

### Where the swarm's name reaches the agent

- **The preamble.** `swarm()` builds each member's preamble at run time in `_preamble()`: the name and roster size, then `Your role: {role}.` when the member has a role, the budget split sentence, the sandbox sentence and each channel's `instructions(member)` (`src/inspect_swarm/_swarm.py:104-113`, `:132-161`). `SwarmRun.start()` appends it as the last user message of the member's input (`src/inspect_swarm/_controller/_control.py:333`). Rendered with `.venv/bin/python` for `MemberInfo("react-1", "reviewer")` in a swarm of four with `filesystem` and `shared_memory`, it opens `You are react-1, one of 4 agents in a swarm working on this task at the same time.` and the filesystem paragraph ends `Keep your own work in /scratch/react-1, which no other member uses.` A swarm of one says `You are react-1, the only agent in this swarm.`
- **The budget split sentence** (`split_sentence()`, `src/inspect_swarm/_budget.py:223-239`) and the sandbox sentences (`_swarm.py:148-156`) contain no names.
- **The `filesystem` channel** is instructions only: no tools, no records, no state (`src/inspect_swarm/_channel/filesystem.py`). Its instructions name the member's scratch directory, `scratch.format(member=member.name)` (`:31`), from a template that must contain `{member}` and no other field (`:74-84`). With `shared_volume=True` there is no scratch line (`:21-26`).
- **The shared-memory channel** shows the swarm's names in most results: a viewed file's fence carries `writer` (`src/inspect_swarm/_channel/shared_memory.py:908-914`), listing lines name each file's writer (`:938-940`, `_listing_line` at `:1110-1111`), a deleted file reads `deleted by {writer}` (`:901`), a stale write is refused with `last written by {writer}` (`:799`), and notices and wait results say `by {writers}` (`:430-433`, `:471-473`). The writer is the record's sender, the member's name (`:384`). Its `check()` requires names in an addressing grammar because they appear in fences and notices (`:46-47`, `:103-108`).
- **`swarm_wait`**, which comes with a channel that sets `max_wait` (only `shared_memory` today), names ended members (`{member} ended ({status}).`, `src/inspect_swarm/_bus/_wait.py:361-362`) and its own member (`{name} is ending`, `{name} has ended.`, `{name} is already waiting`, `:157`, `:165`, `:372`). The shared-memory tool's own errors also name the caller (`{member} has ended.`, `shared_memory.py:569`). The bus's address resolution names members in its errors (`src/inspect_swarm/_bus/_bus.py:236-252`), for the later messages channel.
- **Notices** (`swarm_on_continue()`) are built from the channels' `notice_lines()` (`src/inspect_swarm/_bus/_notice.py:63-67`); only channels with records have notice lines.
- **Nowhere else.** `swarm_resources()` reports amounts only (`src/inspect_swarm/_tools.py:18-50`). The member's tool-state scope, agent span and timeline are named after it but never sent to a model. A member's own sandbox may be named after it (`sandbox="{member}"`, `src/inspect_swarm/_member.py:122-123`); the swarm never shows the sandbox name, though the sample's sandbox configuration could (a compose `hostname:`, an environment variable).

So with the filesystem channel alone, the preamble's first sentence and the scratch path are the only places the swarm tells a member its name; with any channel that has records, the bus and the channel's tools show every member's name to every member.

### What the swarm records per member

`MemberInfo` (`name`, `role`) is what a controller's `check` and a channel's `check()` and `instructions()` see (`src/inspect_swarm/_controller/_controller.py:14-22`); it is public (`src/inspect_swarm/__init__.py:31`). `swarm()` builds it from each record (`_swarm.py:67-71`), and the bus builds it again for `ChannelRun.roster` (`_bus.py:66-68`). The result record's `MemberResult` holds `name`, `role`, `state`, `status`, `submitted`, `time`, `sandbox` and the gate counts (`src/inspect_swarm/_record.py:12-43`), built by `MemberRun.result()` (`_control.py:223-235`). The ledger's `MemberLedger` holds the name, status and usage (`src/inspect_swarm/_ledger.py:89-115`). `Member` serialises every field into the plan step (`_member.py:177-195`), which `task_identifier()` hashes, so two arms that differ in a member field get different eval-set identifiers (`tests/test_api_log.py:287-300` checks this for `delivery`).

### Rules this design keeps

- A name in the swarm's records and tools "is an identifier the eval author chose; members never choose names" ([swarm-api.md Members](swarm-api.md#members), the Names bullet).
- Notices contain only "names the eval author or controller assigned", never "a name a member chose" ([swarm.md Delivery](swarm.md#delivery-peer-messages-are-model-output)); the `NoticeLine` docstring says the same (`src/inspect_swarm/_channel/_channel.py:99-100`).

### Who uses what

inspect_swarm has no users outside this repository yet: no other Inspect or Meridian repository checked out beside it imports it (searched 2026-10-22). Its channels are the built-in `filesystem` and `shared_memory`, and the user channels in its tests; one of those, `_Locks` in `tests/test_channel.py:37-39`, puts `member.name` in its instructions. Controllers are the built-in `leaderless`, which starts every member with the swarm's input and names no one to them (`src/inspect_swarm/_controller/leaderless.py:33-40`). The design therefore serves the filesystem channel (with and without `shared_volume`), `channels=[]`, and instructions-only user channels, and refuses channels with records.

## Design

### At a glance

```python
swarm(
    member(react, args={"tools": [bash()]}, count=4, choose_name=True),
)
```

runs four members, `react-1` to `react-4` in the log, each told: `You are one of 4 agents in a swarm working on this task at the same time. The swarm has not given you a name: choose one for yourself, and tell the other members what it is.` followed by the filesystem instructions with `Keep your own work in /scratch/<your name>, a directory named with the name you chose; no other member should use the same one.`

### The option

`Member` and `member()` gain one field, after `role`:

```python
choose_name: bool = False
"""Whether the member chooses its own name. `True` leaves the member's name out of its preamble, which asks it to choose a name and tell the other members; the swarm still uses `name` for everything it records."""
```

It is per member, beside `name` and `role`, which it qualifies. A whole swarm without assigned names is `choose_name=True` on each record (one record with `count` for identical members). A roster may mix the two, for example a lead the eval author names and workers that name themselves; the named member's name is in its own preamble only, so nothing reveals it to the others.

`member()`'s docstring gets a paragraph, "Chosen names": what the member is and is not told, that the swarm's records still use `name`, that a channel with records cannot be used with it, and that the eval author's own text (the sample input, a controller's `start(input=...)`, the agent's prompt, the sandbox configuration) is not changed. The `name` argument's description adds: "With `choose_name=True` the member is not told it."

`Member._serialize()` writes `choose_name` after `role`, always, as it writes every other field. A record given as data without the field validates with `False`.

### The preamble

`_preamble()` keeps its signature; it reads `member.choose_name` from the `MemberInfo` it already takes. Only the first paragraph changes:

| | `choose_name=False` (unchanged) | `choose_name=True` |
|---|---|---|
| Several members | `You are {name}, one of {N} agents in a swarm working on this task at the same time.` | `You are one of {N} agents in a swarm working on this task at the same time. The swarm has not given you a name: choose one for yourself, and tell the other members what it is.` |
| One member | `You are {name}, the only agent in this swarm.` | `You are the only agent in this swarm. The swarm has not given you a name: choose one for yourself.` |

The rest of the preamble is as today. **The role line stays**: `Your role: {role}.` is the eval author's assignment of what the member does, not who it is, and a study of naming wants roles held fixed while names vary. An eval that wants no role leaves `role` unset. (The issue's question; recommended, and listed under [Open questions](#open-questions).)

The sentence asks the member to choose and announce a name, rather than saying nothing, because "members have to choose a name for themselves and tell their peers what it is" is the issue's condition, and because the filesystem channel's scratch convention needs a name to refer to. How the member tells the others is up to it: the notes file, a file in the shared directory, or nothing.

### `MemberInfo`

`MemberInfo` gains `choose_name: bool = False` as its last field, so existing positional and keyword constructions keep working:

```python
choose_name: bool = False
"""Whether the member chooses its own name. Then nothing the member is told may contain `name`: a channel's `instructions()` must not use it, and `swarm()` refuses a channel whose instructions do."""
```

`swarm()` sets it from the record (`_swarm.py:67-71`). The bus builds `ChannelRun.roster` from the runtime's `MemberHandle`s (`_bus.py:41-48`, `:66-68`), so `MemberHandle` gains a read-only `choose_name` property beside `role` (`_control.py:52-63`), implemented by `MemberRun.choose_name`, set from the record beside `role` (`_control.py:184-188`), and the bus copies it. A controller's `check` sees it in `MemberInfo` and its `run` in the handle, so a controller that writes a member's input with `start(input=...)` can leave names out for that member.

### The `filesystem` channel

`FilesystemChannel.instructions()` changes only its scratch sentence, and only for a member with `choose_name`:

- Default: `Keep your own work in {scratch.format(member=name)}, which no other member uses.` (unchanged).
- `choose_name=True`: `Keep your own work in {scratch.format(member="<your name>")}, a directory named with the name you chose; no other member should use the same one.` With the default template that is `/scratch/<your name>`; with `scratch="/tmp/{member}/work"` it is `/tmp/<your name>/work`.

The channel cannot promise the directory is unused, as it does for assigned names, because two members can choose the same name; saying so is part of the condition being studied. With `shared_volume=True` there is no scratch sentence and nothing changes. `filesystem()`'s docstring for `scratch` adds: "For a member that chooses its own name, `{member}` is shown as `<your name>`."

### Refusals

Two checks in `swarm()`, at construction, so an arm that cannot hide names fails when the task is built, not in a sample:

1. **Channels with records.** In `_check_bus_channels()` (`_swarm.py:273-307`), beside the bridged refusal, for the first record with `choose_name` when any channel has a record name:

   > `swarm(): member 'react-1' chooses its own name (choose_name=True), and channels with records, such as shared_memory, show members to each other by the names the swarm gave them. Leave the channel out, or set choose_name=False.`

   The rule covers every channel with records, not only `shared_memory`, because the bus's own output names members: `swarm_wait` results, notices and address errors come from the bus, and the later messages channel addresses members by name. It applies to the whole swarm, not just the member that chooses, because a peer's view of the board shows the chooser's assigned name as the writer of its files.

2. **Instructions that depend on the name.** A new `_check_chosen_names(channels, roster)`, called after `_check_bus_channels()`. For each member with `choose_name` and each channel, it calls `channel.instructions(info)` and `channel.instructions(dataclasses.replace(info, name=_PROBE))`, where `_PROBE = "#/"` cannot be a member name (explicit names refuse both characters, `_member.py:157-163`, and a default name has no `/`). If the two differ, the text depends on the name, and `swarm()` raises:

   > `swarm(): member 'react-1' chooses its own name (choose_name=True), and the instructions of channel_test_locks depend on the member's name, so they would tell it the name. A channel's instructions() must not use member.name when member.choose_name is True.`

   The comparison is exact (no search for the name in the text, so no false match on a name like `solver` appearing in prose) and needs nothing from the channel but deterministic instructions, which `Channel` already requires by holding configuration only (`_channel.py:27-33`). `filesystem` passes it. A channel's instructions cannot otherwise be checked, and a channel that reads `choose_name` is the only way to opt in, so a user channel written before this design fails loudly instead of revealing the name.

`swarm()`'s `Raises: ValueError` list adds both cases.

### The record

`MemberResult` gains `choose_name: bool = False`, after `role`, filled by `MemberRun.result()`. Analysis that reads `inspect_swarm:result` can then split members by condition without decoding the plan step. Nothing else records the option: the ledger is about spend, and the `member_result` evidence keeps its fields.

The name a member chose is **not** recorded. The swarm never learns it: the member states it in free text, in a file, a message or not at all, and any extraction by the swarm would be a guess at model output. It is in the member's transcript, where an analysis (an `inspect_scout` scanner, for example) can find it with the rest of how members referred to themselves and each other. A later design that registers chosen names (below) would add a field for them then.

### Errors and edge cases

- `choose_name=True` with a single member: the one-member sentence above.
- `choose_name=True` with `channels=[]`: the preamble is the first paragraph, role and budget sentences only. Members still share the sample's sandbox, as today.
- `choose_name=True` with per-member sandboxes and `filesystem(shared_volume=True)`: allowed; no name appears. `sandbox="{member}"` still names the environment after the swarm's name, which the swarm does not show.
- `choose_name=True` with a bridged member: allowed with `filesystem`; bridged members cannot use channels with records anyway.
- A controller's `start(member, input=...)` is the eval author's text and is used as given.

## Alternatives considered

**A `swarm()` argument.** `swarm(choose_names=True)` states "nobody is assigned a name" in one place, which reads well for the study. But the name and role it qualifies are per member, a mixed roster (a named lead, self-named workers) is a reasonable arm that it cannot express, and the record field is per member anyway. A swarm-wide flag is a loop over records on the per-member one; the reverse is not possible. Not chosen; see [Open questions](#open-questions).

**Registering the chosen name.** A `swarm_name(name)` tool that each self-naming member must call before it can use the channels; the swarm checks the name against the addressing grammar and against names already taken, and channels show the registered name instead of the swarm's (fences, listings, notices, wait results, message addresses), with the record storing both. This is what makes shared memory and the messages channel usable with chosen names. It costs a new tool and a gate on every channel tool, a second name per member through the bus, its evidence and every channel, a rule for what peers see before a member registers (a placeholder id, which is an assigned name again), and it breaks the rule that notices never contain a name a member chose ([swarm.md Delivery](swarm.md#delivery-peer-messages-are-model-output)), which would need its own security argument (a chosen name is model output, constrained by the grammar but still chosen to be read by peers). It also changes the condition being studied: names become a swarm-enforced, unique registry rather than something the members work out. No arm needs it with the filesystem channel; left for a later design if a study needs shared memory or messages with chosen names.

**Anonymous ids in channels with records.** Show other members as opaque ids (`m-3`) and the member itself as `you`. The ids are assigned names by another spelling, so they defeat the condition, and every channel would need a per-viewer renaming of its output. Not chosen.

**Drop the scratch sentence for self-naming members.** Simpler than the `<your name>` form, but members would then have no convention for private work in a shared sandbox, which is a different condition from the named arm in more than the name. Not chosen.

**Search the instructions for the name instead of the probe.** Simpler to state, but a default name such as `solver` or `agent` can appear in a channel's prose and would be refused falsely, while a channel that shows part of the name would pass. The probe call is exact. Not chosen.

## Compatibility and migration

- **Public API.** `member(choose_name=)`, `Member.choose_name`, `MemberInfo.choose_name` and `MemberHandle.choose_name` are additive (`MemberHandle` is a protocol the runtime implements; controllers only read it) with a `False` default; existing calls, positional `MemberInfo(name, role)` included, are unchanged, and so are their preambles.
- **Channels.** A user channel that uses `member.name` in its instructions keeps working for members that do not choose their names, and is refused at `swarm()` for those that do.
- **Logs and task identifiers.** The plan step's member dict gains `"choose_name": false`. As with `delivery` and `sandbox` before it, a task's `task_identifier()` changes on upgrade even with unchanged arguments, so an eval set compares arms run on one inspect_swarm version. Replaying an older log, whose member dicts lack the field, gives `False`.
- **Result record.** `MemberResult` gains a defaulted field in record version 1. Older records read with `False`; an older inspect_swarm reading a newer record ignores the field (`MemberResult` keeps pydantic's default `extra="ignore"`). No viewer types are generated from it.
- **Other designs.** This design amends [swarm-api.md](swarm-api.md) (the Names bullet and the preamble line in Security) to say that a member with `choose_name` is not told its name and may choose one for itself, while the swarm's names stay the eval author's. The proposed, unimplemented [swarm-sandbox-isolation.md](swarm-sandbox-isolation.md) names each member's user `swarm-<member>` and its home after the member in the preamble; when it is implemented it must refuse `choose_name` members or describe their user without the name (see [Not this design](#not-this-design)).

## Security

- **No new untrusted input reaches the swarm.** The option is the eval author's configuration. The name a member chooses stays in model output (files, messages, the transcript) and never enters text the swarm generates: not the preamble, not notices, not fences, not records. The rules in [Rules this design keeps](#rules-this-design-keeps) hold unchanged.
- **Refusing channels with records** is what keeps it so. A design that let chosen names through the bus would have to treat them as model output wherever they appear ([Alternatives considered](#alternatives-considered)).
- **The probe** calls a channel's `instructions()` with a member name of `#/`. A channel's instructions are trusted eval-author code that already receives member names; the probe value is a fixed string, used only to compare two results and never shown to a model.
- **Collisions are not a security boundary.** Two members may choose the same name and use the same scratch directory. The filesystem channel is a convention, not isolation, today as before ([swarm-communication.md The filesystem channel](swarm-communication.md#the-filesystem-channel)).

## Testing

All in inspect_swarm, with mockllm and stub agents, on asyncio and trio, with no network or Docker:

- `tests/test_channel.py`: the preamble of a `choose_name=True` member, for two members and for one, with and without a role, with `filesystem()` (default and a custom `scratch`), `filesystem(shared_volume=True)` (with member sandboxes, as `tests/test_member_sandboxes.py` builds them) and `channels=[]`: it contains the new first sentence, the role line when set, `<your name>` in the scratch sentence, and none of the member's assigned names (`react-1`, ..., checked against every member's name, not just its own). A mixed roster gives the named member the unchanged preamble. The existing `preamble_recorder` reads the name from the preamble, so the new cases record by the member's `MemberContext` name or by call order instead.
- `tests/test_channel.py`: `swarm()` refuses `channel_test_locks()` (whose instructions use `member.name`) beside a `choose_name` member with the message above, and accepts it when no member chooses; a channel that reads `member.choose_name` and leaves the name out is accepted.
- `tests/test_shared_memory_channel.py`: `swarm()` refuses `shared_memory()` with a `choose_name` member, and with a mixed roster where only one member chooses; the message names the member and the channel.
- `tests/test_api_log.py`: the logged member dict includes `"choose_name"` (`test_logged_members`), replay rebuilds it (`test_replay_rebuilds_the_swarm`), and a `choose_name` arm in `test_eval_set_arms_are_distinct`'s parameters gives two arms differing only in it distinct identifiers.
- `tests/test_member.py`: a member given as a dict without `choose_name` validates with `False`.
- The result record: a swarm run with one choosing and one named member writes `choose_name` per member in `inspect_swarm:result` (in `tests/test_controller.py`, beside the existing record checks).

## Implementation plan

One pull request, `feat: members that choose their own names`:

1. `src/inspect_swarm/_member.py`: `Member.choose_name`, `member(choose_name=)` with its docstring paragraph and argument, `_serialize()`.
2. `src/inspect_swarm/_controller/_controller.py`: `MemberInfo.choose_name`. `src/inspect_swarm/_controller/_control.py`: `MemberHandle.choose_name`, `MemberRun.choose_name`, `MemberRun.result()`. `src/inspect_swarm/_bus/_bus.py`: the roster's `MemberInfo` carries it. `src/inspect_swarm/_record.py`: `MemberResult.choose_name`.
3. `src/inspect_swarm/_swarm.py`: the roster's `MemberInfo` carries it; `_preamble()`'s first paragraph; the refusal in `_check_bus_channels()`; `_check_chosen_names()`; `swarm()`'s `Raises`.
4. `src/inspect_swarm/_channel/filesystem.py`: the scratch sentence and the `scratch` docstring.
5. `README.md`: a short paragraph under the member options that `member(..., choose_name=True)` keeps the assigned name from the member, works with the filesystem channel and not with shared memory.
6. The tests above. The stand-in members that implement `MemberHandle` in `tests/test_bus.py`, `tests/test_shared_memory.py` and `tests/test_wait.py` gain `choose_name = False`.

## Open questions

1. **Per member or per swarm?** Recommendation: per member (`member(choose_name=True)`), as above; it expresses the all-self-named arm with one flag per record and also a mixed roster. A `swarm()` flag can be added later as shorthand if sweeps want it.
2. **Keep the role line?** Recommendation: keep it. Roles are the author's assignment of function; a study that wants members without roles leaves `role` unset.
3. **Is shared memory needed for the first study?** Recommendation: no, start with the filesystem channel, which this design supports. If a study needs the board or messages with chosen names, that is the registration design under [Not this design](#not-this-design).

## Not this design

- **Registering chosen names** with the swarm (a `swarm_name` tool), so that channels with records show them and the record stores them beside the assigned name. See [Alternatives considered](#alternatives-considered).
- **A silent variant** that leaves the name out without asking the member to choose one, to see whether members name themselves unprompted. It would be a second value of the option (for example `choose_name="silent"`); no arm asks for it yet.
- **Per-member users** ([swarm-sandbox-isolation.md](swarm-sandbox-isolation.md), proposed): its preamble paragraph names the member's user and home after the member, so its implementation must decide what `choose_name` members are told.
- **Names in the eval author's own text**: the sample input, a controller's `start(input=...)`, the member agent's prompt, and sandbox configuration that names services after members. The swarm cannot see these and does not try.
