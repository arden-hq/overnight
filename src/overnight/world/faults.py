import random
from collections.abc import Collection, Iterable, Mapping

from overnight.domain.contracts import FaultSpec, Invocation
from overnight.domain.vocabulary import WILDCARD


class FaultPlan:
    def __init__(
        self, faults: Iterable[FaultSpec], operations: Mapping[str, Collection[str]], seed: int
    ) -> None:
        self.specs = tuple(faults)
        self.remaining = {fault.id: fault.remaining for fault in self.specs}
        self.random = random.Random(seed)
        if len(self.remaining) != len(self.specs):
            raise ValueError("fault IDs must be unique and nonempty")
        if any(fault.service_id not in operations for fault in self.specs):
            raise ValueError("fault targets unknown service")
        for fault in self.specs:
            if fault.operation != WILDCARD and fault.operation not in operations[fault.service_id]:
                raise ValueError(f"fault targets unknown operation: {fault.operation}")

    def consume(self, invocation: Invocation, now: int) -> FaultSpec | None:
        for fault in self.specs:
            if self.remaining[fault.id] == 0:
                continue
            if not _matches_target(fault, invocation):
                continue
            if not _active_at(fault, now):
                continue
            if self.random.random() < fault.probability:
                self.remaining[fault.id] -= 1
                return fault
        return None


def _matches_target(fault: FaultSpec, invocation: Invocation) -> bool:
    if fault.service_id != invocation.service_id:
        return False
    return fault.operation in (invocation.operation, "*")


def _active_at(fault: FaultSpec, now: int) -> bool:
    if fault.start is not None and now < fault.start:
        return False
    if fault.end is not None and now > fault.end:
        return False
    return True
