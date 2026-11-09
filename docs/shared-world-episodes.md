# Shared-world episodes

An episode runs several tasks against one existing world, clock, execution policy, event queue, and trace. Tasks in different chats are logically interleaved in declaration order. Tasks in the same chat queue behind its active task, preserving one conversation/receipt owner; `start_at` is the earliest admission time. This is deterministic scheduling, not threaded model execution.

Add `tasks` to a scenario. Each task has a unique `id`, an explicit `chat_id`, an initial `message`, optional absolute `start_at` and `deadline` times within the scenario horizon, a `memory` mode (`retain` by default), and private `checks`. The episode ends when every task is done or reaches its deadline, or the scenario reaches its step/horizon bound. One task's `done` only completes that task.

A task is `pending` until it starts, then `active`. A harness task whose native turn ended is `idle`: it is observed again only when a notification for its chat exists or its native wake comes. An idle task completes as `done` when its deadline arrives, when the next task in its chat is due, or when no task is active and nothing scheduled (an event, reminder, operator step or task start) can reach any idle task before its deadline or the horizon. An active task that reaches its deadline becomes `deadline`. A task that ends leaves nothing behind in its chat: its open `wait_for_notification` calls time out, and the harness's pending work and native wake are cancelled, so none of it reaches the chat's next task.

Each task start opens its chat's notification inbox: notifications posted before the start are not offered to it. An operator message for a task that has not started is in the chat's transcript at once and becomes a notification when the task starts, unless the task resets the chat's memory.

Each agent observation contains:

- `task`: the active task ID, chat ID, and deadline;
- the shared `time`, `horizon`, world observation, operations, and policy revision;
- messages visible in that chat since its latest real reset;
- `notifications` for that chat that the agent has not received yet;
- reports and receipts attributable to the active task.

Authored checks never enter observations. A task's checks run against the world snapshot captured when that task completes. This makes an end-of-task claim distinct from the scenario's final-world checks. Task trace checks see entries attributed to that task. Unresolved approvals and authored pending events belonging to the task keep its verdict unknown, with one exception: a deadline retires the task's undecided approvals, which can then never be granted, so they do not count. A task that reaches its deadline is graded by its own checks, and an episode whose tasks all finished or reached their deadline is a finished run, not one cut off by its bound. A scenario can therefore test that an agent leaves something undone: hold its approval, let the deadline retire it, and check that nothing changed. Unrelated operator steps and events do not pollute its verdict.

The result retains the existing scenario fields and adds `task_outcomes`. Each outcome records task/chat identity, terminal status, completion time, step count, unresolved approvals/events, and its private verdict. A failed, invalid, or unresolved checked task prevents an episode pass. A matched forbidden check or broken collateral check gives the episode `failed`, even if the run later turned invalid. A task's false claim gives `failed` once that task finishes, unless the run turns invalid. A clean completed task with no authored checks is neutral.

Actions may omit task/chat IDs because the active turn binds them. If supplied, they must match. Invocation IDs remain unchanged and globally unique across task/chat owners; cross-task reuse is rejected. The action gets a failed receipt under a new ID, so it cannot read or change the other task's receipts. A native harness cannot match that receipt to its call, so it fails closed. Approvals therefore cannot cross task identity.

`run_scenario(..., adapters={chat_id: adapter})` accepts caller-owned per-chat adapters. A single injected adapter is accepted only for a one-chat episode. Without injected adapters, the runner creates and closes one adapter per chat, including failure paths, and collects command connector provenance across chats. Tasks in the same chat share the adapter and retained chat messages. `memory: "reset"` clears the projected chat history and calls the adapter's explicit reset: a persistent command process is closed and restarted, with provenance retained for every generation; a scripted adapter rewinds its plan. A one-shot command already starts a fresh process for every exchange, so reset has no additional process-local state to clear. Adapter reset does not claim to erase external files or service-side memory. A task waits while another task in its chat is active, whether or not it resets the chat; it starts once that task is done or expires. Deadline expiry retires pending native work through the adapter cancellation contract before the chat can admit a successor; an adapter unable to cancel that work fails closed. Expiry does not undo completed world effects or erase the expired task's unresolved approval evidence.

## Examples

`examples/shared-world-episode.json` is a small scripted episode. `examples/shared-world-chaos.json` runs several tasks through a deterministic command oracle (`examples/adapters/episode_oracle.py`) with a seeded variation:

```sh
overnight run examples/shared-world-chaos.json \
  --variation examples/shared-world-chaos-variation.json \
  --operator-answers examples/shared-world-chaos-operator.jsonl \
  --out .local/runs/shared-world-chaos
overnight inspect .local/runs/shared-world-chaos --task catering --kind result
```

The oracle exercises the backend contracts; it is not a model. `--operator-answers` replays authored operator answers; without it the operator persona calls its model. The run adds `task-outcomes.json` to the ordinary [run evidence](evidence.md).
