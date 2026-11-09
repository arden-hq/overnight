import asyncio
import errno
import hashlib
import json
import os
import shutil
import signal
import subprocess
import sys
import threading
from contextlib import AbstractContextManager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from tempfile import mkdtemp
from types import TracebackType
from typing import Callable, Protocol, Self, runtime_checkable

import anyio
from anyio.abc import Process
from anyio.from_thread import BlockingPortal, start_blocking_portal
from anyio.streams.buffered import BufferedByteReceiveStream
from pydantic import TypeAdapter, ValidationError

from overnight.domain.contracts import (
    Action,
    Agent,
    AgentContainer,
    CommandAgent,
    HarnessAgent,
    JsonObject,
    ScriptedAgent,
    action_contract,
)
from overnight.domain.transport import (
    COMMAND_TIMEOUT_SECONDS,
    GRACEFUL_EXIT_SECONDS,
    PERSISTENT_TIMEOUT_SECONDS,
    TERMINATE_WAIT_SECONDS,
    UNRUNNABLE_EXIT_CODES,
    USAGE_PATH_VARIABLE,
)
from overnight.domain.vocabulary import FailureKind
from overnight.execution.container import (
    AGENT_DIRECTORY,
    CONTAINER_PYTHON,
    DOCKER_FAILED,
    ContainerUnavailable,
    check_mounts,
    container_command,
    egress_variables,
    image_id,
    keep_agent_folder,
    new_name,
    own_folder,
    start_egress,
    stop_egress,
)
from overnight.execution.container import remove as remove_container
from overnight.execution.container import sweep as sweep_containers
from overnight.execution.usage import usage_path

MAX_RESPONSE_BYTES = 1_048_576
json_object = TypeAdapter(JsonObject)


@dataclass(frozen=True)
class _ReaderStopped:
    error: BaseException | None = None


class AdapterError(RuntimeError):
    pass


class AgentBehaviorError(AdapterError): ...


class ActionRejected(AgentBehaviorError):
    def __init__(self, message: str, action_id: str | None = None) -> None:
        super().__init__(message)
        self.action_id = action_id


class ContractError(AdapterError): ...


class ExchangeInterrupted(AdapterError): ...


class AgentAdapter(Protocol):
    def respond(self, observation: JsonObject) -> JsonObject: ...


@runtime_checkable
class ResettableAgentAdapter(AgentAdapter, Protocol):
    def reset(self) -> None: ...


def _leaf_message(error: BaseException) -> str:
    if isinstance(error, BaseExceptionGroup):
        return "; ".join(_leaf_message(item) for item in error.exceptions)
    return f"{type(error).__name__}: {error}"


def describe_failure(error: Exception) -> JsonObject:
    failed_in_agent = isinstance(error, AgentBehaviorError)
    return {
        "kind": str(FailureKind.AGENT if failed_in_agent else FailureKind.INFRASTRUCTURE),
        "retryable": not isinstance(error, ContractError),
        "type": type(error).__name__,
        "message": _leaf_message(error),
    }


def next_action(adapter: "AgentAdapter", observation: JsonObject) -> Action:
    reply = adapter.respond(observation)
    try:
        return action_contract.validate_python(reply)
    except ValueError as error:
        details = (
            error.errors(include_url=False, include_input=False)
            if isinstance(error, ValidationError)
            else []
        )
        detail = "; ".join(f"{'.'.join(map(str, item['loc']))}: {item['msg']}" for item in details)
        action_id = reply.get("id")
        raise ActionRejected(
            f"invalid agent action: {detail or error}",
            action_id if isinstance(action_id, str) and action_id else None,
        ) from error


def _decode_response(line: bytes) -> JsonObject:
    try:
        value = json_object.validate_json(line, strict=True)
        json.dumps(value, allow_nan=False)
        return value
    except ValueError as error:
        raise AgentBehaviorError(f"agent response must be a JSON object: {error}") from error


def _encode_observation(observation: JsonObject) -> bytes:
    return (json.dumps(observation, ensure_ascii=False, allow_nan=False) + "\n").encode()


class ManagedAdapter(AbstractContextManager):
    def respond(self, observation: JsonObject) -> JsonObject:
        raise NotImplementedError

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()

    def close(self) -> None:
        pass


class ScriptedAdapter(ManagedAdapter):
    def __init__(self, actions: list[JsonObject]) -> None:
        self.actions = actions
        self.position = 0

    def respond(self, observation: JsonObject) -> JsonObject:
        if self.position >= len(self.actions):
            return {"kind": "wait"}
        action = self.actions[self.position]
        self.position += 1
        return action

    def reset(self) -> None:
        self.position = 0


class CommandAdapter(ManagedAdapter):
    def __init__(
        self,
        command: list[str],
        timeout_seconds: float = COMMAND_TIMEOUT_SECONDS,
        provenance_path: Path | None = None,
        container: AgentContainer | None = None,
    ) -> None:
        self.command = command
        self.timeout_seconds = timeout_seconds
        self.provenance_path = provenance_path
        self.provenance_paths: list[Path] = []
        self.container = container
        self.image_id: str | None = None
        self._exchanges = 0

    def respond(self, observation: JsonObject) -> JsonObject:
        path = None
        if self.provenance_path is not None:
            path = self.provenance_path.with_name(
                f"{self.provenance_path.stem}-{self._exchanges}.json"
            )
            self._exchanges += 1
        with PersistentCommandAdapter(
            self.command, self.timeout_seconds, path, self.container
        ) as process:
            try:
                return process.respond_once(observation)
            finally:
                self.provenance_paths.extend(process.provenance_paths)
                self.image_id = process.image_id or self.image_id

    def reset(self) -> None: ...


class PersistentCommandAdapter(ManagedAdapter):
    def __init__(
        self,
        command: list[str],
        timeout_seconds: float = PERSISTENT_TIMEOUT_SECONDS,
        provenance_path: Path | None = None,
        container: AgentContainer | None = None,
    ) -> None:
        self.command = command
        self.timeout_seconds = timeout_seconds
        self.provenance_path = provenance_path
        self.provenance_paths: list[Path] = []
        self.container = container
        self.container_name: str | None = None
        self._egress_log: Path | None = None
        self.image_id: str | None = None
        self._mounted: Path | None = None
        self._kept: Path | None = None
        self._generation = 0
        self.process: Process | None = None
        self._stdout: BufferedByteReceiveStream | None = None
        self._portal_context: AbstractContextManager[BlockingPortal] | None = None
        self._portal: BlockingPortal | None = None
        self._closed = False
        self._failed = False
        self.process_chat_id: str | None = None
        self._portal_lock = threading.Lock()
        self._write_lock: anyio.Lock | None = None
        self._buffered_replies: dict[str, JsonObject] = {}
        self._reply_events: dict[str, anyio.Event] = {}
        self._reader_task: asyncio.Task[None] | None = None
        self._reader_error: BaseException | None = None
        self._reader_stopped: _ReaderStopped | None = None
        self._untagged_replies: asyncio.Queue[JsonObject | _ReaderStopped] | None = None
        self._response_errors: dict[str, AdapterError] = {}

    def _variables(self, provenance: Path | None) -> dict[str, str]:
        variables = {"OVERNIGHT_PYTHON": CONTAINER_PYTHON if self.container else sys.executable}
        if self.container is not None:
            variables["HOME"] = AGENT_DIRECTORY
        if self.process_chat_id is not None:
            variables["OVERNIGHT_CHAT_ID"] = self.process_chat_id
            variables["OVERNIGHT_CHAT_NAMESPACE"] = hashlib.sha256(
                self.process_chat_id.encode()
            ).hexdigest()
        if provenance is not None:
            variables["OVERNIGHT_PROVENANCE_PATH"] = str(provenance)
            variables[USAGE_PATH_VARIABLE] = str(usage_path(provenance))
        return variables

    async def _container_command(self) -> list[str]:
        assert self.container is not None
        try:
            check_mounts(self.container)
            self.image_id = await image_id(self.container.image)
            await sweep_containers()
        except ContainerUnavailable as error:
            if error.retryable:
                raise AdapterError(str(error)) from error
            raise ContractError(str(error)) from error
        folder = Path(mkdtemp(prefix="overnight-agent-"))
        own_folder(folder)
        self._mounted = folder
        self._kept = None
        if self.provenance_path is not None:
            self._kept = self.provenance_path.with_name(
                f"{self.provenance_path.stem}-generation-{self._generation}"
            )
            self.provenance_paths.append(self._kept / "provenance.json")
        self.container_name = new_name()
        self._egress_log = None
        variables = self._variables(Path(PurePosixPath(AGENT_DIRECTORY, "provenance.json")))
        network = "none"
        if self.container.allow_hosts:
            self._egress_log = folder / "egress.log"
            try:
                network = await start_egress(self.container_name, self.container)
            except ContainerUnavailable as error:
                raise AdapterError(str(error)) from error
            variables |= egress_variables()
        return container_command(
            self.container,
            self.command,
            name=self.container_name,
            agent_directory=folder,
            variables=variables,
            network=network,
        )

    async def _start_process(self) -> None:
        environment = os.environ.copy()
        for key in ("OVERNIGHT_CHAT_ID", "OVERNIGHT_CHAT_NAMESPACE"):
            environment.pop(key, None)
        if self.container is not None:
            try:
                await self._open(await self._container_command(), environment)
            except BaseException:
                with anyio.CancelScope(shield=True):
                    await self._remove_container()
                raise
            return
        else:
            command = self.command
            path = None
            if self.provenance_path is not None:
                path = self.provenance_path.with_name(
                    f"{self.provenance_path.stem}-generation-{self._generation}.json"
                )
                self.provenance_paths.append(path)
            environment.update(self._variables(path))
        await self._open(command, environment)

    async def _remove_container(self) -> None:
        if self.container_name is None:
            return
        # A killed docker client leaves its container running.
        await remove_container(self.container_name)
        if self._egress_log is not None:
            await stop_egress(self.container_name, self._egress_log)
        self.container_name = None
        if self._mounted is not None:
            mounted, self._mounted = self._mounted, None
            try:
                if self._kept is not None:
                    keep_agent_folder(mounted, self._kept)
            finally:
                shutil.rmtree(mounted, ignore_errors=True)

    async def _open(self, command: list[str], environment: dict[str, str]) -> None:
        try:
            self.process = await anyio.open_process(
                command,
                env=environment,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=None,
                start_new_session=True,
            )
        except OSError as error:
            if isinstance(error, (FileNotFoundError, NotADirectoryError, PermissionError)) or (
                error.errno == errno.ENOEXEC
            ):
                raise ContractError(f"agent command cannot start: {error}") from error
            raise AdapterError(f"agent command could not start: {error}") from error
        assert self.process.stdout is not None
        self._stdout = BufferedByteReceiveStream(self.process.stdout)
        self._untagged_replies = asyncio.Queue()
        self._reader_task = asyncio.create_task(self._read_replies())

    async def _finish_one_shot_response(self) -> None:
        assert self.process is not None
        assert self._untagged_replies is not None
        trailing = await self._untagged_replies.get()
        if not isinstance(trailing, _ReaderStopped):
            raise AgentBehaviorError("agent command must write exactly one JSON line")
        if trailing.error is not None:
            raise AgentBehaviorError(
                "agent command must write exactly one JSON line"
            ) from trailing.error
        returncode = await self.process.wait()
        if returncode:
            raise AgentBehaviorError(f"agent command exited {returncode}")

    async def _read_replies(self) -> None:
        assert self._stdout is not None
        try:
            while True:
                line = await self._stdout.receive_until(b"\n", MAX_RESPONSE_BYTES)
                received = _decode_response(line)
                message_id = received.pop("message_id", None)
                if message_id is None:
                    assert self._untagged_replies is not None
                    await self._untagged_replies.put(received)
                    continue
                if not isinstance(message_id, str):
                    raise AgentBehaviorError("agent response has invalid message_id")
                self._buffered_replies[message_id] = received
                event = self._reply_events.get(message_id)
                if event is not None:
                    event.set()
        except anyio.IncompleteRead as caught:
            assert self._stdout is not None
            error = caught if self._stdout.buffer else None
        except anyio.EndOfStream:
            error = None
        except BaseException as caught:
            error = caught
        stopped = _ReaderStopped(error)
        self._reader_stopped = stopped
        if self._untagged_replies is not None:
            await self._untagged_replies.put(stopped)
        if error is not None:
            self._reader_error = error
        for event in self._reply_events.values():
            event.set()

    async def _next_untagged_reply(self) -> JsonObject:
        if self._reader_error is not None:
            raise self._reader_error
        assert self._untagged_replies is not None
        if self._reader_stopped is not None and self._untagged_replies.empty():
            raise AgentBehaviorError("agent command ended before response")
        received = await self._untagged_replies.get()
        if isinstance(received, _ReaderStopped):
            if received.error is not None:
                raise received.error
            raise AgentBehaviorError("agent command ended before response")
        return received

    async def _fail_response(self, response_id: str, reason: str) -> None:
        self._response_errors[response_id] = ExchangeInterrupted(reason)
        event = self._reply_events.get(response_id)
        if event is not None:
            event.set()

    async def _wait_for_correlated_reply(self, response_id: str) -> JsonObject:
        event = anyio.Event()
        self._reply_events[response_id] = event
        try:
            if (
                self._reader_stopped is None
                and response_id not in self._buffered_replies
                and response_id not in self._response_errors
            ):
                await event.wait()
            if self._reader_error is not None:
                raise self._reader_error
            if error := self._response_errors.pop(response_id, None):
                raise error
            if response_id not in self._buffered_replies:
                raise AgentBehaviorError("agent command ended before response")
            return self._buffered_replies.pop(response_id)
        finally:
            self._reply_events.pop(response_id, None)

    async def _unrunnable(self, error: AgentBehaviorError) -> AdapterError:
        if self.process is None:
            return error
        with anyio.move_on_after(1):
            await self.process.wait()
        code = self.process.returncode
        if self.container is not None and code == DOCKER_FAILED:
            return AdapterError(f"docker could not run the agent container: {error}")
        if code in UNRUNNABLE_EXIT_CODES:
            return ContractError(f"agent command could not run (exit {code}): {error}")
        return error

    async def _exchange(
        self,
        observation: JsonObject,
        require_exit: bool,
        response_id: str | None = None,
        on_sent: Callable[[], None] | None = None,
    ) -> JsonObject:
        try:
            return await self._exchange_once(observation, require_exit, response_id, on_sent)
        except AgentBehaviorError as error:
            raise await self._unrunnable(error) from error

    async def _exchange_once(
        self,
        observation: JsonObject,
        require_exit: bool,
        response_id: str | None,
        on_sent: Callable[[], None] | None,
    ) -> JsonObject:
        try:
            with anyio.fail_after(self.timeout_seconds):
                if self.process is None:
                    await self._start_process()
                assert self.process is not None
                assert self.process.stdin is not None and self._stdout is not None
                stdin = self.process.stdin
                if self._write_lock is None:
                    self._write_lock = anyio.Lock()

                async def send_observation() -> None:
                    assert self._write_lock is not None
                    async with self._write_lock:
                        await stdin.send(_encode_observation(observation))
                        if require_exit:
                            await stdin.aclose()

                await send_observation()
                if on_sent is not None:
                    on_sent()
                if response_id is None:
                    reply = await self._next_untagged_reply()
                else:
                    reply = await self._wait_for_correlated_reply(response_id)
                if require_exit:
                    await self._finish_one_shot_response()
                return reply
        except TimeoutError as error:
            raise AgentBehaviorError("persistent agent exchange timed out") from error
        except (
            OSError,
            anyio.EndOfStream,
            anyio.IncompleteRead,
            anyio.DelimiterNotFound,
            anyio.BrokenResourceError,
            ExceptionGroup,
        ) as error:
            raise AgentBehaviorError(
                f"persistent agent protocol failed: {_leaf_message(error)}"
            ) from error

    def respond(self, observation: JsonObject) -> JsonObject:
        return self._request_response(observation, require_exit=False)

    def respond_once(self, observation: JsonObject) -> JsonObject:
        return self._request_response(observation, require_exit=True)

    def reset(self) -> None:
        if self._closed:
            raise AdapterError("persistent agent is closed")
        if self._portal is not None:
            self._portal.call(self._close_process)
        self._generation += 1

    def _request_response(
        self,
        observation: JsonObject,
        *,
        require_exit: bool,
        response_id: str | None = None,
        on_sent: Callable[[], None] | None = None,
    ) -> JsonObject:
        if self._closed:
            raise AdapterError("persistent agent is closed")
        with self._portal_lock:
            if self._portal is None:
                self._portal_context = start_blocking_portal()
                self._portal = self._portal_context.__enter__()
        try:
            return self._portal.call(
                self._exchange, observation, require_exit, response_id, on_sent
            )
        except ExchangeInterrupted:
            raise
        except AdapterError:
            self._failed = True
            self.close()
            raise

    async def _signal_group(self, sig: signal.Signals) -> None:
        assert self.process is not None
        try:
            os.killpg(self.process.pid, sig)
        except ProcessLookupError:
            return
        except PermissionError:
            # Observed on Darwin during leader exit. Reap within the teardown budget, then retry
            # the group so surviving descendants still receive the signal.
            with anyio.move_on_after(TERMINATE_WAIT_SECONDS):
                await self.process.wait()
            if self.process.returncode is None:
                raise
            try:
                os.killpg(self.process.pid, sig)
            except ProcessLookupError:
                return

    async def _close_process(self) -> None:
        if self.process is None:
            return
        process = self.process
        try:
            with anyio.CancelScope(shield=True):
                try:
                    if self._reader_task is not None:
                        self._reader_task.cancel()
                        await asyncio.gather(self._reader_task, return_exceptions=True)
                        self._reader_task = None
                    if process.stdin is not None:
                        await process.stdin.aclose()
                    if not self._failed:
                        with anyio.move_on_after(GRACEFUL_EXIT_SECONDS):
                            await process.wait()
                    await self._signal_group(signal.SIGTERM)
                    with anyio.move_on_after(TERMINATE_WAIT_SECONDS):
                        await process.wait()
                    await self._signal_group(signal.SIGKILL)
                finally:
                    with anyio.move_on_after(TERMINATE_WAIT_SECONDS):
                        await process.aclose()
                    await self._remove_container()
        finally:
            self.process = None
            self._stdout = None
            self._write_lock = None
            self._buffered_replies.clear()
            self._reply_events.clear()
            self._reader_error = None
            self._reader_stopped = None
            self._untagged_replies = None
            self._response_errors.clear()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            if self._portal is not None:
                self._portal.call(self._close_process)
        finally:
            if self._portal_context is not None:
                self._portal_context.__exit__(None, None, None)
            self._portal = None
            self._portal_context = None


def make_adapter(config: Agent, provenance_path: Path | None = None) -> ManagedAdapter:
    match config:
        case HarnessAgent():
            from overnight.execution.harness import HarnessAdapter

            return HarnessAdapter(
                config.command,
                config.timeout_seconds,
                provenance_path,
                config.required,
                config.container,
            )
        case ScriptedAgent():
            return ScriptedAdapter(
                [action.model_dump(mode="json", exclude_unset=True) for action in config.actions]
            )
        case CommandAgent(kind="persistent_command"):
            return PersistentCommandAdapter(
                config.command,
                PERSISTENT_TIMEOUT_SECONDS
                if config.timeout_seconds is None
                else config.timeout_seconds,
                provenance_path,
                config.container,
            )
        case CommandAgent():
            return CommandAdapter(
                config.command,
                COMMAND_TIMEOUT_SECONDS
                if config.timeout_seconds is None
                else config.timeout_seconds,
                provenance_path,
                config.container,
            )
