# Configuration – Inspect Swarm

## Records

[member()](../reference/inspect_swarm.html.md#member) returns a [Member](../reference/inspect_swarm.configuration.html.md#member), and `swarm(budget=...)` takes a [Budget](../reference/inspect_swarm.configuration.html.md#budget). They are on their own page because [Member](../reference/inspect_swarm.configuration.html.md#member) and [member()](../reference/inspect_swarm.html.md#member) would share a heading anchor on one page.

### Member

A swarm member: a built agent, or a registered `@agent` factory and its arguments.

Create one with [member()](../reference/inspect_swarm.html.md#member), which documents the fields and the member contract.

[Source](https://github.com/meridianlabs-ai/inspect_swarm/blob/3cd681e3033bd04f3ee457036c33169ad76fe786/src/inspect_swarm/_member.py#L87)

``` python
class Member(BaseModel)
```

#### Attributes

`agent` [Agent](https://inspect.aisi.org.uk/reference/inspect_ai.agent.html#agent) \| None  
A built, registered agent. Exactly one of `agent` and `factory` is set.

`factory` AgentFactory \| None  
A registered `@agent` factory, called with `args` to build the member’s agent.

`args` dict\[str, Any\]  
The factory’s keyword arguments.

`name` str \| None  
The member’s name. `None` uses the agent’s or factory’s unqualified registry name.

`role` str \| None  
The member’s role, given to the controller and in the member’s preamble.

`count` int  
How many members this record stands for. Above 1 needs the factory form.

`limits` list\[[Limit](https://inspect.aisi.org.uk/reference/inspect_ai.util.html#limit)\]  
The member’s own limits.

`bridged` bool \| None  
Whether the member’s agent is bridged. `None` counts agents in the `inspect_swe/` namespace as bridged.

`delivery` Literal\['notify', 'poll'\] \| None  
The member’s configured delivery mode: `notify` or `poll`. `None` selects `notify`. In `notify` mode, [swarm_on_continue()](../reference/inspect_swarm.html.md#swarm_on_continue) adds notices to the member’s conversation; in `poll` mode it adds none, and the member learns of changes from the status line that ends a shared memory result and from `swarm_wait` results.

`sandbox` str \| None  
The member’s own sandbox environment, where `{member}` stands for the member’s name. `None` shares the sample’s sandbox.

`visible_sandboxes` list\[str\]  
Other sandbox environments the member can reach by name, such as a shared service. Only with `sandbox`.

### Budget

The swarm-wide budget.

By default the swarm derives its caps from the sample’s cost, token and time limits when it starts, holding back `reserve` of each. The caps are stopping rules: when one is reached the swarm stops its members, gives them up to `grace` seconds to finish the calls they have in flight, and then cancels the members still running. If the sample has a time limit, the grace period is capped at half its remaining time.

Every field is a plain value, so each can be a task parameter, and two budgets that differ in any field are logged as different arguments.

[Source](https://github.com/meridianlabs-ai/inspect_swarm/blob/3cd681e3033bd04f3ee457036c33169ad76fe786/src/inspect_swarm/_budget.py#L32)

``` python
class Budget(BaseModel)
```

#### Attributes

`cost` float \| None  
Swarm cost cap in dollars. `None` derives it from the sample’s cost limit.

`tokens` int \| str \| None  
Swarm token cap. A string such as `"output:1m"` is parsed by `parse_token_limit()` and keeps its metering type. `None` derives it from the sample’s token limit, with that limit’s metering type.

`time` float \| None  
Seconds after which the swarm stops its members. `None` derives it from the sample’s time limit.

`reserve` float  
Fraction of each sample limit held back when a cap is derived.

`split` Literal\['pool', 'equal'\] \| tuple\[float, ...\]  
How the cost and token caps are divided between members: one shared pool, equal shares, or one weight per member in roster order. A member that uses up its share stops, and its unused share is not given to the others.

`grace` float  
Seconds members have, at most, to finish calls already in flight after a stop. If the sample has a time limit, this period is capped at half its remaining time.
