# Services

Each module in `src/overnight/services/` (`mail`, `chat`, `calendar`, `shop`, `records`, and the world's own `system`) owns its state models, argument models and handlers. A handler is pure: it takes one service instance's state and an invocation and returns a `Transition` with the new state, a result, any emissions and any notices. It never performs I/O, schedules time or writes the trace. The world checks the actor's grants and faults and commits the transition.

Every service instance in a scenario has its own `id` and one of the kinds below. Dates are integer simulation seconds, except calendar `start`/`end`, which are ISO 8601 strings with UTC offsets. Generated IDs use the first unused numbered ID in that service (`mail-1`, `order-1`). Seed IDs must be unique.

Values are stored in one spelling, for seeds and writes alike, so a check compares values rather than how they were typed:

- Text (IDs, a mail subject, a calendar title, a mail or chat body) is stored without surrounding whitespace; inner text, line breaks included, is kept. Text that is only whitespace is rejected, except a mail `body`, which may be empty.
- Name sets are stored sorted: mail `to` and `cc`, calendar `attendees`, chat `participants` and records `visible_to`. `["person", "operator"]` is stored as `["operator", "person"]`.
- Records fields are free JSON unless a field rule applies (below); their top-level text values are stored without surrounding whitespace.

| Kind | Seed `state` | Operations and arguments |
| --- | --- | --- |
| `mail` | `messages: [{id, from, to: [actor ID or address], cc?, subject, body, date, draft?}]` | `list_messages` (`folder?`: inbox/sent/drafts, `query?`); `read_message` (`id`); `send_message` (`to`, `subject`, `body`, `cc?`); `save_draft` (`subject`, `body`, `to?`, `cc?`); `delete_messages` (`ids`) |
| `chat` | `conversations: [{id, participants: [actor IDs], messages: [{id, from, body, date}]}]` | `list_conversations`; `read_messages` (`conversation_id`); `start_conversation` (`participants`, `body`); `send_message` (`conversation_id`, `body`) |
| `calendar` | `events: [{id, owner_id, title, start, end, attendees?, location?}]` | `list_events` (`from?`, `to?`); `create_event` (`title`, `start`, `end`, `attendees?`, `location?`); `update_event` (`id`, optional event fields); `delete_event` (`id`) |
| `shop` | `catalog: [{id, title, price_cents, available?}], orders?: [{id, item_id, buyer_id, quantity, total_cents, status, placed_at}]` | `list_items`; `buy` (`item_id`, `quantity?`); `list_orders`; `cancel_order` (`order_id`) |
| `records` | `collections: {name: {records: [{id, owner_id, visible_to?, ...fields}], add?: {required?, final?}, update?, delete?, fields?: {field: {type?, values?}}}}` | `list_records` (`collection`); `get_record` (`collection`, `id`); `add_record` (`collection`, `fields`, `visible_to?`); `update_record` (`collection`, `id`, `fields`); `delete_record` (`collection`, `id`) |

## Visibility

Read operations return only rows the actor may see. Broad access to a service never lets an actor read another person's private row by ID.

- Mail: inbox visibility follows exact `to`/`cc` actor IDs; sent mail and drafts follow `from`. Deleting inbox mail hides it only for that recipient. Addresses are simulation labels, not delivery targets.
- Chat: conversation membership is required.
- Calendar: attendees can read; only the owner can change an event.
- Shop: the catalog is shared; orders belong to their buyer. Seed orders must name an item in that catalog; `available` is the stock after seeded orders. Cancelling an active order restores its quantity once.
- Records: readable by the owner and explicit `visible_to` actors; only the owner may update or delete. `add.final` is strictly boolean.

### Records field rules

A collection's `fields` map gives a field a rule: `type` is one of `text`, `integer`, `number`, `boolean` or `time` (an ISO 8601 time with a UTC offset), and `values` lists the values the field may hold. `integer` rejects `"40500"`, `40500.5` and booleans; `number` accepts integers and decimals; a `values` entry is compared like a check's `eq`, so a listed time matches every spelling of its moment. Every value in `values` must fit `type` when both are given. A rule cannot name `id`, `owner_id` or `visible_to`.

`add_record` and `update_record` fail when a written field breaks its rule, and the error names the field and what fits, for example `Field status must be one of: "cancelled", "kept"`, so the agent can correct the call. Seed records must fit their collection's rules. Fields without a rule stay free JSON. Rules are world setup: an observation lists a collection's records, not its rules.

Sent mail and chat messages become world state immediately; replies from people are ordinary scripted events. `records` offers bounded collection operations, not a general storage API.

## Notifications

A handler declares who its write concerns with `context.notify(recipients, kind, ref, summary)`. After the transition commits, the world posts a notification to each recipient that has the agent role and can see the service (it reads it or holds a grant on it). Failed, denied and faulted calls notify no one. A notification's id is derived from its cause (the invocation id, `reminder:<id>` or `operator:<message id>`), its recipient and, for a copy to the invoker, the chat it goes to, so posting it again is a no-op.

A notification goes to the recipient's chat that last wrote in the same thread (same service and `ref`, such as a chat conversation or a shop order); with no such chat it has `chat_id: null` and reaches every open chat of the recipient. A call never notifies the chat that made it. When the invoker is a recipient and the call came from a chat, the invoker is notified in the chat that last wrote the thread before this call if that is another chat, nowhere if it is the calling chat, and otherwise once in each of its other open chats (each copy with its own id and `chat_id`). A call without a chat (a single-chat trial) never notifies its invoker. The call then makes its chat the invoker's last writer of the thread, so the agent's repeated messages in one conversation from one chat stay out of its other chats; only its first write to a thread from a chat, or a write after another chat last wrote that thread, reaches them.

| Handler | Recipients | Kind | `ref` |
| --- | --- | --- | --- |
| `mail.send_message` | distinct `to` and `cc` | `mail_received` | mail id |
| `chat.send_message`, `chat.start_conversation` | conversation participants | `chat_message` | conversation id |
| `calendar.create_event`, `update_event`, `delete_event` | attendees; attendees before and after; attendees before | `calendar_changed` | event id |
| `records.add_record`, `update_record`, `delete_record` | the record's `visible_to` | `record_changed` | `collection/id` |
| `shop.cancel_order` | the order's buyer | `order_changed` | order id |
| a reminder coming due | its owner, in the reminder's chat | `reminder_due` | reminder id |
| an operator message | the agent, in the message's chat | `operator_message` | message id |

`shop.buy` creates an order without changing a status, and `mail.save_draft`, `mail.delete_messages` and reads change nothing anyone else sees, so they notify no one.

## System

The world binds one `system` service for every run; scenarios cannot declare, grant, fault or schedule it. Every agent actor sees it in its world view (`{as_of, reminders}`, its own reminders) and can call it under any operator policy.

| Operation | Arguments | Result |
| --- | --- | --- |
| `current_time` (read only) | none | `{time, horizon}` |
| `wait_for_notification` | `timeout` (seconds, at least 1) | deferred until a notification for the caller's chat exists or `min(now + timeout, horizon)` arrives: `{time, until, timed_out, notifications}` |
| `set_reminder` | `message`, and exactly one of `at` (absolute) or `delay` (seconds, at least 1) | `{id, due, created}` |
| `list_reminders` (read only) | none | `{reminders}`: the caller's own, in every status |
| `cancel_reminder` | `id` | `{id, status, changed}` |

A reminder must be due after now and no later than the horizon. It is `scheduled` until it fires (`fired`, posting one `reminder_due` notification at its due time) or is cancelled (`cancelled`). Setting a scheduled reminder again with the same chat, due time and message returns the existing id with `created: false`. Cancelling a reminder that already fired or was cancelled succeeds with `changed: false`; an unknown id or another actor's reminder fails.

A wait that finds undelivered notifications for the caller's chat returns them at once. Otherwise its receipt stays `waiting` and the call blocks: the world settles it when a notification arrives or its timeout (bounded by the horizon, or the task's deadline) comes, and the notifications it returns count as delivered. A wait times out at once when the caller's turn or task ends, so it never takes a notification meant for a later turn.

## Schemas

The registry exposes an `OperationSpec` with a JSON input schema and a `read_only` flag for each operation. Each spec binds a Pydantic argument model to its handler, so the same model generates the agent-facing schema and validates the actual dispatch. Event argument templates are validated against that schema. Send operations require at least one recipient; draft recipients are optional.
