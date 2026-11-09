import asyncio
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass

from pydantic import TypeAdapter

from overnight.domain.contracts import InvokeAction, JsonObject, receipt_contract
from overnight.domain.vocabulary import PolicyOutcome, ResultStatus
from overnight.execution.tool_server import ToolCatalog, TrustedToolContext

catalog_contract = TypeAdapter(ToolCatalog)
objects_contract = TypeAdapter(list[JsonObject])


class OperationCancelled(RuntimeError): ...


@dataclass
class PendingInvocation:
    action: InvokeAction
    identity: TrustedToolContext
    result: asyncio.Future[JsonObject]


class ToolBridge:
    """Accept concurrent submissions; dispatch one operation at a time in FIFO order."""

    def __init__(self, chat_id: str, record: Callable[[JsonObject], None]):
        self.chat_id = chat_id
        self.record = record
        self.run_id = ""
        self.pending: PendingInvocation | None = None
        self.seen: set[tuple[str, str]] = set()
        self._waiting: deque[PendingInvocation] = deque()
        self._available = asyncio.Event()

    @property
    def has_pending(self) -> bool:
        return self.pending is not None or bool(self._waiting)

    async def invoke(
        self,
        service: str,
        operation: str,
        arguments: JsonObject,
        identity: TrustedToolContext | None,
    ) -> JsonObject:
        if identity is None or identity.chat_id != self.chat_id or identity.run_id != self.run_id:
            raise ValueError("missing or wrong trusted invocation context")
        key = (identity.run_id, identity.tool_call_id)
        if key in self.seen:
            raise ValueError("tool call was already dispatched; receipts are not replayable")
        self.seen.add(key)
        invocation = PendingInvocation(
            action=InvokeAction(
                kind="invoke",
                id=f"{identity.run_id}:{identity.tool_call_id}",
                service_id=service,
                operation=operation,
                args=arguments,
                chat_id=self.chat_id,
            ),
            identity=identity,
            result=asyncio.get_running_loop().create_future(),
        )
        self._waiting.append(invocation)
        self._available.set()
        try:
            return await invocation.result
        finally:
            if invocation in self._waiting:
                self._waiting.remove(invocation)
                if not self._waiting:
                    self._available.clear()

    async def next_action(self) -> JsonObject:
        if self.pending is not None:
            raise RuntimeError("resolve the pending operation before dispatching another")
        while True:
            await self._available.wait()
            invocation = self._waiting.popleft()
            if not self._waiting:
                self._available.clear()
            if not invocation.result.cancelled():
                break
        self.pending = invocation
        self.record(
            {
                "kind": f"{invocation.identity.identity_source}_dispatch",
                **invocation.identity.model_dump(mode="json", exclude_none=True),
                "invocation_id": invocation.action.id,
                "service": invocation.action.service_id,
                "operation": invocation.action.operation,
            }
        )
        return invocation.action.model_dump(mode="json", exclude_unset=True)

    def resolve(self, receipts: object) -> JsonObject | None:
        receipts = objects_contract.validate_python(receipts)
        pending = self.pending
        if pending is None:
            return None
        matches = [receipt for receipt in receipts if receipt.get("id") == pending.action.id]
        if not matches:
            raise ValueError("runner did not return the pending invocation receipt")
        raw = matches[-1]
        receipt = receipt_contract.validate_python(raw)
        if receipt.invocation_id != pending.action.id:
            raise ValueError("runner receipt belongs to another invocation")
        if receipt.status in (PolicyOutcome.PENDING, ResultStatus.WAITING):
            return {"kind": "wait"}
        if receipt.status == PolicyOutcome.APPROVED:
            return pending.action.model_dump(mode="json", exclude_unset=True)
        if receipt.status not in (ResultStatus.SUCCEEDED, ResultStatus.FAILED, ResultStatus.DENIED):
            raise ValueError("runner receipt does not resolve the operation")
        self.record({"kind": f"{pending.identity.identity_source}_receipt", "receipt": raw})
        self.pending = None
        if not pending.result.done():
            pending.result.set_result(raw)
        return None

    def cancel_pending(self) -> None:
        """Release active and queued callers; dispatched effects are not rolled back."""
        invocations = list(self._waiting)
        self._waiting.clear()
        self._available.clear()
        if self.pending is not None:
            invocations.insert(0, self.pending)
            self.pending = None
        for invocation in invocations:
            if not invocation.result.done():
                invocation.result.set_exception(
                    OperationCancelled("World operation cancelled by adapter")
                )
        if invocations:
            self.record(
                {
                    "kind": "adapter_calls_cancelled",
                    "invocation_ids": [invocation.action.id for invocation in invocations],
                }
            )
