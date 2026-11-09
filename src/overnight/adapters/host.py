import asyncio
import json
import os
import sys
import threading
from collections.abc import Awaitable, Callable
from typing import Literal

from overnight.adapters.protocol import Hello, Lifecycle, Observe, Steer, request_contract
from overnight.execution.adapters import MAX_RESPONSE_BYTES
from overnight.execution.harness import (
    ActionReply,
    Capabilities,
    LifecycleReply,
    Ready,
    SteerAck,
)

type ObserveHandler = Callable[[Observe], Awaitable[ActionReply]]
type LifecycleHandler = Callable[[Lifecycle], Awaitable[LifecycleReply]]
type SteerHandler = Callable[[Steer], Awaitable[None]]
type ReplyWriter = Callable[[object, str | None], Awaitable[None]]
type SteeringReason = Literal[
    "inactive", "target_mismatch", "duplicate", "unsupported", "native_rejected"
]


class _Router:
    def __init__(
        self,
        capabilities: Capabilities,
        observe: ObserveHandler,
        lifecycle: LifecycleHandler,
        steer: SteerHandler | None,
        write: ReplyWriter,
    ) -> None:
        self.capabilities = capabilities
        self.observe = observe
        self.lifecycle = lifecycle
        self.steer = steer
        self.write = write
        self.greeted = False
        self.chat_id: str | None = None
        self.active: tuple[str, str] | None = None
        self.observe_task: asyncio.Task[None] | None = None
        self.steering_tasks: set[asyncio.Task[None]] = set()
        self.seen_steering_requests: set[str] = set()

    async def run_observe(self, message: Observe) -> None:
        target = (message.session_id, message.turn_id)
        try:
            reply = await self.observe(message)
            if self.active != target:
                return
            self.active = None
            await self.settle_steering()
            await self.write(reply, message.message_id)
        finally:
            if self.active == target:
                self.active = None

    async def settle_steering(self) -> None:
        if self.steering_tasks:
            await asyncio.gather(*tuple(self.steering_tasks))

    def steering_reason(self, message: Steer) -> SteeringReason | None:
        target = (message.session_id, message.turn_id)
        if message.request_id in self.seen_steering_requests:
            return "duplicate"
        self.seen_steering_requests.add(message.request_id)
        if not self.capabilities.steering or self.steer is None:
            return "unsupported"
        if self.active is None:
            return "inactive"
        if self.active != target:
            return "target_mismatch"
        return None

    async def run_steer(self, message: Steer) -> None:
        reason = self.steering_reason(message)
        accepted = False
        if reason is None:
            try:
                assert self.steer is not None
                await self.steer(message)
                accepted = True
            except Exception:
                reason = "native_rejected"
        await self.write(
            SteerAck(
                kind="steer_ack",
                request_id=message.request_id,
                session_id=message.session_id,
                turn_id=message.turn_id,
                accepted=accepted,
                reason=reason,
            ),
            message.message_id,
        )

    async def start_observe(self, message: Observe, tasks: asyncio.TaskGroup) -> None:
        if not self.capabilities.steering:
            raise ValueError("correlated observe requires steering capability")
        if message.session_id is None or message.turn_id is None:
            raise ValueError("correlated observe requires a session and turn")
        if self.active is not None:
            raise ValueError("connector already has an active turn")
        self.active = (message.session_id, message.turn_id)
        self.observe_task = tasks.create_task(self.run_observe(message))

    async def start_steer(self, message: Steer, tasks: asyncio.TaskGroup) -> None:
        task = tasks.create_task(self.run_steer(message))
        self.steering_tasks.add(task)
        task.add_done_callback(self.steering_tasks.discard)

    async def run_lifecycle(self, message: Lifecycle) -> bool:
        self.active = None
        if self.observe_task is not None and not self.observe_task.done():
            self.observe_task.cancel()
            await asyncio.gather(self.observe_task, return_exceptions=True)
        self.observe_task = None
        await self.settle_steering()
        reply = await self.lifecycle(message)
        if reply.kind != message.kind or reply.scope != message.scope:
            raise ValueError("connector lifecycle acknowledgement does not match")
        await self.write(reply, None)
        return message.kind != "close"

    def bind(self, message: Observe | Steer | Lifecycle) -> None:
        if self.chat_id is not None and self.chat_id != message.chat_id:
            raise ValueError("connector host is bound to one chat")
        self.chat_id = message.chat_id

    async def route(self, message, tasks: asyncio.TaskGroup) -> bool:
        if isinstance(message, Hello):
            if self.greeted:
                raise ValueError("duplicate handshake")
            self.capabilities.require(message.required)
            self.greeted = True
            await self.write(Ready(kind="ready", version=1, capabilities=self.capabilities), None)
            return True
        if not self.greeted:
            raise ValueError("handshake required")
        self.bind(message)
        if isinstance(message, Steer):
            await self.start_steer(message, tasks)
        elif isinstance(message, Observe) and message.message_id is not None:
            await self.start_observe(message, tasks)
        elif isinstance(message, Observe):
            await self.write(await self.observe(message), None)
        else:
            return await self.run_lifecycle(message)
        return True


def _stdin_reader() -> asyncio.StreamReader:
    loop = asyncio.get_running_loop()
    reader = asyncio.StreamReader(limit=MAX_RESPONSE_BYTES + 1)
    descriptor = sys.stdin.fileno()

    def pump() -> None:
        try:
            while chunk := os.read(descriptor, 65_536):
                loop.call_soon_threadsafe(reader.feed_data, chunk)
            loop.call_soon_threadsafe(reader.feed_eof)
        except OSError as error:
            loop.call_soon_threadsafe(reader.set_exception, error)
        except RuntimeError:
            return

    threading.Thread(target=pump, name="connector-stdin", daemon=True).start()
    return reader


async def _read_requests(router: _Router) -> None:
    reader = _stdin_reader()
    async with asyncio.TaskGroup() as tasks:
        while True:
            try:
                line = await reader.readline()
            except ValueError as error:
                raise ValueError("connector request exceeds the exchange limit") from error
            if not line:
                break
            if len(line) > MAX_RESPONSE_BYTES:
                raise ValueError("connector request exceeds the exchange limit")
            if not await router.route(request_contract.validate_json(line), tasks):
                break


async def serve(
    capabilities: Capabilities,
    observe: ObserveHandler,
    lifecycle: LifecycleHandler,
    close: Callable[[], Awaitable[None]],
    steer: SteerHandler | None = None,
) -> None:
    """Validate and concurrently route tagged active-turn control messages."""
    output_lock = asyncio.Lock()

    async def write(reply, message_id: str | None) -> None:
        value = reply.model_dump(mode="json")
        if message_id is not None:
            value["message_id"] = message_id
        async with output_lock:
            print(json.dumps(value, ensure_ascii=False, allow_nan=False), flush=True)

    try:
        await _read_requests(_Router(capabilities, observe, lifecycle, steer, write))
    finally:
        await close()
