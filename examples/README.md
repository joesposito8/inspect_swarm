# Examples

Runnable swarms. Each file defines one task at the bottom. Read them in this order:

| Example | What it shows | Needs |
|---------|---------------|-------|
| `getting_started.py` | `k` `react()` members on each question; the first submission is the answer | |
| `shared_memory.py` | The shared-memory channel: members split a count, `append` their parts to a board, and wait for each other with `swarm_wait` | |
| `stop_on_proof.py` | Writing a controller: `until_proved()` stops the swarm at the first checked answer, and a member builder that makes a normal return mean a passed check | |
| `budget_sweep.py` | Agent count, budget and split as task parameters; the `swarm_resources()` tool | Cost data for the model |
| `shared_sandbox.py` | Members with `bash()` in one shared container, the `filesystem` channel, and a scorer that reads what they left | Docker |
| `proof_swarm.py` | A verifier-gated swarm shaped like Epoch AI's LeanOpenProblems port: a container per member, a shared checking service that reads each member's workspace, a shared-memory board, `until_proved()`, and a scorer that checks each member's answer | Docker |
| `stop_on_outage_tools.py` | Stopping on the state of the world: three ops agents upgrade a database cluster held in the store, and `until_outage()` stops the swarm the moment it loses quorum. A wrapper around the tool that can lose quorum wakes the controller | |
| `stop_on_outage_turns.py` | The same controller, woken from `react(on_continue=...)` after each turn | |
| `stop_on_outage_event.py` | The same stop with `swarm_notify()`: the tool queues a `MemberNotified` event, and the controller loops over `events()` with no side channel | |
| `stop_on_outage_world.py` | The check at the change: each tool checks quorum as it changes the cluster and records a loss in the store, so a server restarted while another is down still stops the swarm, which the wakes above miss | |
| `stop_on_outage_service.py` | The cluster as a service in its own container that checks quorum on every change and logs it, `bash()` members in their own containers, and a controller that blocks on the log | Docker |
| `stop_on_outage_bridged.py` | The same world and controller with bridged members: a CLI agent in each container, through `sandbox_agent_bridge()`, whose commands leave no `ToolEvent`s | Docker, `openai` |
| `stop_on_outage_sidecar.py` | The cluster as files on a shared volume, with a watcher beside the world that records each change as it reads it, and what such a watcher cannot see | Docker |

`questions.py` holds the questions that `getting_started.py`, `stop_on_proof.py` and `budget_sweep.py` share. `compose.yaml` and `proof_swarm.yaml` are the Docker configurations of `shared_sandbox.py` and `proof_swarm.py`. `cluster_outage.py` holds the cluster, its tools and `until_outage()` for the four outage examples that need no sandbox; `cluster_service.py`, `cluster_client.py` and `cluster_agent.py` run inside the containers of `stop_on_outage_service.yaml`, and `cluster_watcher.py` inside `stop_on_outage_sidecar.yaml`.

## Running

Install Inspect Swarm ([installation](../README.md#installation)) and your model provider's SDK, then run an example from the repository root:

```bash
inspect eval examples/getting_started.py --model openai/gpt-5-mini
```

Task parameters go in `-T`:

```bash
inspect eval examples/getting_started.py --model openai/gpt-5-mini -T k=1
inspect eval examples/stop_on_proof.py --model openai/gpt-5-mini -T stop=all_done
inspect eval examples/budget_sweep.py --model openai/gpt-5-mini -T k=8 -T budget=8 -T split=equal
```

Each example caps its samples with a token or cost limit, from which the swarm takes its budget.

## Tests

`tests/test_examples.py` runs every example with scripted mockllm members. The ones that need no sandbox run in CI. The others need Docker, so their tests are marked slow and run with `pytest --runslow`; `stop_on_outage_bridged.py`'s also needs the `openai` package, and skips without it.
