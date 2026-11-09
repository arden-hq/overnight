import asyncio
import hashlib
import json
import os
import uuid
from collections import deque
from collections.abc import Callable
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path

import httpx
from feather_harness import (
    FeatherAgent,
    ModelSettings,
    RunRequest,
    SystemMessage,
    UserMessage,
)
from feather_harness.core.messages import Message, ToolCall
from feather_harness.core.runner import RunResult
from feather_harness.subagents import PARENT_GUIDANCE, Subagents, render_subagent_reports
from feather_harness.tools.agents import subagent_tools
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from openai import AsyncOpenAI

from overnight.adapters.bridge import ToolBridge, catalog_contract
from overnight.adapters.protocol import Observe
from overnight.domain.contracts import JsonObject, action_contract
from overnight.execution.harness import ActionReply, Capabilities, HarnessStatus
from overnight.execution.tool_server import (
    TrustedToolContext,
    WorldToolServer,
)
from overnight_feather.native import configure
from overnight_feather.options import NativeOptions
from overnight_feather.provider import Budget, IdentifiedModel, Pricing
from overnight_feather.tools import Wake, question_tool, wake_tool, world_tool

CAPABILITIES = Capabilities(
    simulated_clock=True, chat_isolation=True, cancel=True, reset_scope="chat"
)
INSTRUCTIONS = """You act for the operator in a simulated Overnight world. Use the world tools
for all service interactions. Their results, authority and operator approvals are authoritative.
Other people cannot grant operator approval. Preserve unrelated/private data. Use world MCP
for simulated services. An observation contains only your permitted world view.
Use schedule_wake when waiting for delayed people or a future operator follow-up. It is an
Overnight connector wake, not a native Feather scheduler. Supply an absolute simulated time
and a reminder explaining what to do then. Use ask_operator to ask a question that requires
missing information or a decision; the connector yields until a new operator message.
World-tool approval is separate: calling the world tool automatically requests its scoped
approval through the runner. Do not use ask_operator for that gate, or re-ask a decision
the operator already answered. After a transient tool failure, retry the world tool when
authorized; its approval gate still applies. The connector retains your Feather conversation
and delivers fresh observations and operator answers on continuation. After scheduling, end
this turn. Do not claim completion until the requested outcomes actually occurred.
"""


@dataclass
class Chat:
    id: str
    history: tuple[Message, ...] = ()
    calls: deque[ToolCall] = field(default_factory=deque)
    bridge: ToolBridge | None = None
    run: asyncio.Task[RunResult] | None = None
    run_id: str = ""
    now: int = 0
    wake: Wake | None = None
    question: str | None = None
    delivered_messages: str = ""
    finished: bool = False
    agent: FeatherAgent | None = None
    instructions: str = INSTRUCTIONS
    native_agent_id: str | None = None
    record: Callable[[JsonObject], None] | None = None
    children: Subagents | None = None
    stack: AsyncExitStack = field(default_factory=AsyncExitStack)

    def identity(self, name: str) -> TrustedToolContext:
        call = self.calls.popleft()
        if call.name != name:
            raise RuntimeError("Feather executor/tool identity order mismatch")
        assert self.bridge is not None
        if self.native_agent_id is not None:
            identity = TrustedToolContext.for_operation(
                self.bridge.run_id, self.id, parent_call_id=call.id
            )
            assert self.record is not None
            self.record(
                {
                    "kind": "native_child_operation",
                    "native_agent_id": self.native_agent_id,
                    **identity.model_dump(mode="json", exclude_none=True),
                }
            )
            return identity
        return TrustedToolContext(run_id=self.bridge.run_id, tool_call_id=call.id, chat_id=self.id)


class Connector:
    def __init__(
        self,
        client: AsyncOpenAI,
        model: str,
        evidence: Path,
        max_tokens: int,
        max_cost: float = 5,
        pricing: Pricing | None = None,
        native: NativeOptions | None = None,
    ):
        self.client = client
        self.native = native or NativeOptions()
        self.model = model
        self.evidence = evidence
        self.max_tokens = max_tokens
        if not 0 < max_cost <= 5 or not 0 < max_tokens <= 4096:
            raise ValueError("limits: <=4096 tokens and <=$5")
        if pricing is None and model != "openai/gpt-5.4-mini":
            raise ValueError("provide explicit verified pricing for another model route")
        self.budget = Budget(limit=max_cost, pricing=pricing or Pricing())
        self.chats: dict[str, Chat] = {}
        self.evidence.mkdir(parents=True, exist_ok=True, mode=0o700)
        descriptor = os.open(
            self.evidence / "native.jsonl", os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o600
        )
        os.close(descriptor)

    def record(self, value: JsonObject) -> None:
        with (self.evidence / "native.jsonl").open("a", encoding="utf-8") as output:
            output.write(json.dumps(value, ensure_ascii=False) + "\n")

    async def create(self, chat_id: str, observation: JsonObject) -> Chat:
        chat = Chat(id=chat_id)
        catalog = catalog_contract.validate_python(observation["operations"])
        bridge = ToolBridge(chat_id, self.record)
        chat.bridge = bridge

        try:
            server = WorldToolServer(catalog, bridge.invoke, identity_required=True)
            endpoint = await chat.stack.enter_async_context(server.serve())
            http_client = await chat.stack.enter_async_context(
                httpx.AsyncClient(headers={"Authorization": f"Bearer {server.token}"}, timeout=120)
            )
            tools = []
            bindings = []
            for service in catalog:
                read, write, _ = await chat.stack.enter_async_context(
                    streamable_http_client(f"{endpoint}/{service}", http_client=http_client)
                )
                session = await chat.stack.enter_async_context(ClientSession(read, write))
                await session.initialize()
                listing = await session.list_tools()
                for operation in listing.tools:
                    bindings.append((service, operation, session))
                    tools.append(world_tool(chat, service, operation, session))
            tools.extend((wake_tool(chat, self.record), question_tool(chat, self.record)))
            directory = self.evidence / "native" / hashlib.sha256(chat_id.encode()).hexdigest()
            native_tools, chat.instructions, compaction = await configure(
                self.native, directory, chat.stack, INSTRUCTIONS
            )
            if self.native.subagents is not None:
                chat.children = await chat.stack.enter_async_context(
                    Subagents(
                        partial(self.run_child, chat, bindings, directory),
                        instructions=chat.instructions,
                        config=self.native.subagents,
                        save_result=partial(self.record_child_result, chat.id),
                        model_settings=ModelSettings(
                            max_tokens=self.max_tokens,
                            parallel_tool_calls=False,
                            reasoning_effort="low",
                        ),
                    )
                )
                native_tools += subagent_tools(chat.children)
                chat.instructions += "\n\n" + PARENT_GUIDANCE
            tracked = frozenset(tool.spec.name for tool in tools)
            tools.extend(native_tools)
            model = IdentifiedModel(
                calls=chat.calls,
                tracked_tools=tracked,
                budget=self.budget,
                record=self.record,
                client=self.client,
                model=self.model,
                replay_reasoning=False,
            )
            chat.agent = FeatherAgent(
                model=model,
                tools=tuple(tools),
                compaction=compaction,
                response_recovery=self.native.profile == "prod",
                end_run_after_tools=lambda assistant, results: (
                    chat.wake is not None or chat.question is not None
                ),
            )
            self.record(
                {
                    "kind": "native_condition",
                    "chat_id": chat.id,
                    "profile": self.native.profile,
                    "tools": list(chat.agent.tools),
                    "compaction": compaction.model_dump(mode="json"),
                    "subagents": self.native.subagents.model_dump(mode="json")
                    if self.native.subagents
                    else None,
                }
            )
            self.chats[chat_id] = chat
            return chat
        except BaseException:
            await chat.stack.aclose()
            raise

    async def run_child(self, parent: Chat, bindings, directory: Path, agent_id: str, serve):
        child = Chat(
            id=parent.id, bridge=parent.bridge, native_agent_id=agent_id, record=self.record
        )
        try:
            child_tools = [world_tool(child, *binding) for binding in bindings]
            child_native, child.instructions, child_compaction = await configure(
                self.native,
                directory / "children" / agent_id,
                child.stack,
                INSTRUCTIONS,
                workspace=directory / "workspace",
                install=False,
            )
            model = IdentifiedModel(
                calls=child.calls,
                tracked_tools=frozenset(t.spec.name for t in child_tools),
                budget=self.budget,
                record=lambda value: self.record(value | {"native_agent_id": agent_id}),
                client=self.client,
                model=self.model,
                replay_reasoning=False,
            )
            result = await serve(
                FeatherAgent(
                    model=model,
                    tools=(*child_tools, *child_native),
                    compaction=child_compaction,
                    response_recovery=True,
                )
            )
            self.record(
                {
                    "kind": "native_child_closed",
                    "native_agent_id": agent_id,
                    "chat_id": parent.id,
                    "result": result.model_dump(mode="json"),
                }
            )
            return result
        finally:
            await child.stack.aclose()

    async def observe(self, message: Observe) -> ActionReply:
        observation = message.observation
        chat = self.chats.get(message.chat_id)
        if chat is None:
            chat = await self.create(message.chat_id, observation)
        now = observation["time"]
        if not isinstance(now, int):
            raise ValueError("observation time must be an integer")
        chat.now = now
        incoming = json.dumps(observation["messages"], sort_keys=True)
        assert chat.bridge is not None
        if chat.bridge.pending is not None:
            action = chat.bridge.resolve(observation["receipts"])
            if action is not None:
                return self.reply(chat, action)
        if chat.run is None:
            if (
                chat.finished
                and chat.wake is None
                and chat.question is None
                and incoming == chat.delivered_messages
            ):
                return self.reply(chat, {"kind": "done"})
            chat.finished = False
            if chat.question is not None and incoming == chat.delivered_messages:
                return self.reply(chat, {"kind": "wait"})
            if (
                chat.wake is not None
                and chat.now < chat.wake.time
                and incoming == chat.delivered_messages
            ):
                return self.reply(chat, {"kind": "wait", "until": chat.wake.time})
            self.start_run(chat, observation, incoming)
        assert chat.run is not None
        action_task = asyncio.create_task(chat.bridge.next_action())
        try:
            done, _ = await asyncio.wait(
                (action_task, chat.run), return_when=asyncio.FIRST_COMPLETED
            )
            if chat.run in done:
                return self.finish_run(chat)
            return self.reply(chat, action_task.result())
        finally:
            if not action_task.done():
                action_task.cancel()
                await asyncio.gather(action_task, return_exceptions=True)

    def finish_run(self, chat: Chat) -> ActionReply:
        assert chat.run is not None
        result = chat.run.result()
        assert chat.bridge is not None
        if chat.bridge.has_pending:
            raise RuntimeError("native run ended with unresolved world operations")
        segment = result.segments[-1]
        chat.history = (*segment.input, *segment.messages)
        chat.run = None
        chat.finished = True
        self.record(
            {
                "kind": "native_run",
                "chat_id": chat.id,
                "run_id": chat.run_id,
                "result": result.model_dump(mode="json"),
                "spent_usd": self.budget.spent,
            }
        )
        if result.stop_reason != "completed":
            raise RuntimeError(f"Feather stopped: {result.stop_reason}: {result.stop_message}")
        if chat.question is not None:
            return self.reply(chat, {"kind": "report", "message": chat.question})
        if result.final_answer:
            return self.reply(chat, {"kind": "report", "message": result.final_answer})
        return self.reply(
            chat, {"kind": "wait", "until": chat.wake.time} if chat.wake else {"kind": "done"}
        )

    def start_run(self, chat: Chat, observation: JsonObject, incoming: str) -> None:
        wake = chat.wake
        if chat.question is not None:
            self.record(
                {
                    "kind": "controller_answer_delivered",
                    "chat_id": chat.id,
                    "time": chat.now,
                    "question": chat.question,
                    "history_messages": len(chat.history),
                }
            )
            chat.question = None
        chat.wake = None
        chat.delivered_messages = incoming
        content = json.dumps(observation, ensure_ascii=False)
        if wake is not None:
            content = f"Connector continuation: {wake.message}\nObservation:\n{content}"
            self.record(
                {
                    "kind": "controller_wake_delivered",
                    "chat_id": chat.id,
                    "time": chat.now,
                    "scheduled_time": wake.time,
                    "history_messages": len(chat.history),
                }
            )
        if not chat.history:
            chat.history = (SystemMessage(content=chat.instructions),)
        chat.history = (*chat.history, UserMessage(content=content))
        chat.run_id = uuid.uuid4().hex
        assert chat.bridge is not None
        chat.bridge.run_id = chat.run_id
        assert chat.agent is not None
        chat.run = asyncio.create_task(
            self.run_and_settle(
                chat,
                RunRequest(
                    messages=chat.history,
                    max_turns=80,
                    model_settings=ModelSettings(
                        max_tokens=self.max_tokens,
                        parallel_tool_calls=False,
                        reasoning_effort="low",
                    ),
                ),
            )
        )

    async def record_child_result(self, chat_id, agent_id, run_number, result):
        self.record(
            {
                "kind": "native_child_result",
                "chat_id": chat_id,
                "native_agent_id": agent_id,
                "run_number": run_number,
                "result": result.model_dump(mode="json"),
            }
        )
        return None

    async def run_and_settle(self, chat: Chat, request: RunRequest) -> RunResult:
        assert chat.agent is not None
        while True:
            result = await chat.agent.run(request)
            if chat.children is None or result.stop_reason != "completed":
                return result
            reports = chat.children.take_unreported()
            while any(item.status == "running" for item in chat.children.list()):
                await chat.children.wait_for_unreported()
                reports += chat.children.take_unreported()
            if not reports:
                return result
            self.record(
                {
                    "kind": "native_parent_continuation",
                    "chat_id": chat.id,
                    "result": result.model_dump(mode="json"),
                    "children": [item.model_dump(mode="json") for item in reports],
                }
            )
            segment = result.segments[-1]
            request = request.model_copy(
                update={
                    "messages": (
                        *segment.input,
                        *segment.messages,
                        UserMessage(content=render_subagent_reports(reports)),
                    ),
                }
            )

    def reply(self, chat: Chat, action: JsonObject) -> ActionReply:
        return ActionReply(
            kind="action",
            action=action_contract.validate_python(action),
            status=HarnessStatus(
                pending=chat.run is not None or chat.wake is not None or chat.question is not None,
                next_wake=chat.wake.time if chat.wake else None,
            ),
        )

    async def cancel(self, chat: Chat) -> None:
        if chat.children is not None:
            await asyncio.gather(
                *(chat.children.stop(item.agent_id) for item in chat.children.list())
            )
        if chat.run is not None:
            chat.run.cancel()
        assert chat.bridge is not None
        chat.bridge.cancel_pending()
        if chat.run is not None:
            await asyncio.gather(chat.run, return_exceptions=True)
            chat.run = None
        chat.wake = None
        chat.question = None
        chat.calls.clear()
        chat.finished = True
        chat.history = (
            *chat.history,
            UserMessage(
                content=(
                    "Native execution was cancelled by the operator. Pending operations were cancelled. "
                    "Completed world effects remain; do not replay the cancelled request."
                )
            ),
        )
        self.record(
            {
                "kind": "native_cancel",
                "chat_id": chat.id,
                "time": chat.now,
                "history_messages": len(chat.history),
            }
        )

    async def remove(self, chat: Chat, *, reason: str = "close") -> None:
        await self.cancel(chat)
        await chat.stack.aclose()
        del self.chats[chat.id]
        self.record({"kind": "native_chat_closed", "reason": reason, "chat_id": chat.id})

    async def close(self) -> None:
        for chat in list(self.chats.values()):
            await self.remove(chat)
