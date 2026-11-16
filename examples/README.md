# Examples

Runnable swarms. Each file defines one task at the bottom. Read them in this order:

| Example | What it shows | Needs |
|---------|---------------|-------|
| `getting_started.py` | `k` `react()` members on each question; the first submission is the answer | |
| `shared_memory.py` | The shared-memory channel: members split a count, `append` their parts to a board, and wait for each other with `swarm_wait` | |
| `stop_on_proof.py` | Writing a controller: `until_proved()` stops the swarm at the first checked answer, and a member builder that makes a normal return mean a passed check | |
| `lead_and_workers.py` | Persistent members: a lead hands out tasks on the shared memory, and the controller wakes idle workers on the lead's writes (`MemberNews`) | |
| `budget_sweep.py` | Agent count, budget and split as task parameters; the `swarm_resources()` tool | Cost data for the model |
| `shared_sandbox.py` | Members with `bash()` in one shared container, the `filesystem` channel, and a scorer that reads what they left | Docker |
| `proof_swarm.py` | A verifier-gated swarm shaped like Epoch AI's LeanOpenProblems port: a container per member, a shared checking service that reads each member's workspace, a shared-memory board, `until_proved()`, and a scorer that checks each member's answer | Docker |
| `stop_on_outage.py` | Stopping on the state of the world: a database cluster in the store whose tools report each change with `world_report()`, three ops agents upgrading it, and `until_outage()`, a controller that holds the quorum rule and stops the moment two of them take servers down at once | |
| `stop_on_outage_service.py` | That cluster as a service in its own container, reporting every change through the sandbox service that `world_service()` publishes; `bash()` members in their own containers manage it with `curl`, and the same controller stops the swarm | Docker |

`questions.py` holds the questions that `getting_started.py`, `stop_on_proof.py` and `budget_sweep.py` share. `compose.yaml` and `proof_swarm.yaml` are the Docker configurations of `shared_sandbox.py` and `proof_swarm.py`. `cluster_service.py` and `stop_on_outage_service.yaml` are the service and the Docker configuration of `stop_on_outage_service.py`.

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

`tests/test_examples.py` runs every example with scripted mockllm members. The six that need no sandbox run in CI. `shared_sandbox.py`, `proof_swarm.py` and `stop_on_outage_service.py` need Docker, so their tests are marked slow and run with `pytest --runslow`.
