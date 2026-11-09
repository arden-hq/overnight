import hashlib
import json
from dataclasses import dataclass

from overnight.domain.contracts import (
    Invocation,
    JsonObject,
    Mode,
    PolicyConfig,
    ScenarioError,
)
from overnight.domain.vocabulary import SYSTEM_SERVICE, WILDCARD, PolicyMode, PolicyOutcome


def _invocation_identity(invocation: Invocation) -> str:
    fields = {
        "id": invocation.id,
        "actor_id": invocation.actor_id,
        "service_id": invocation.service_id,
        "operation": invocation.operation,
        "args": invocation.model_dump(mode="json")["args"],
        "task_id": invocation.task_id,
        "chat_id": invocation.chat_id,
    }
    return json.dumps(
        fields, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    )


def _approval_fingerprint(invocation: Invocation, revision: int) -> str:
    return hashlib.sha256(f"{revision}:{_invocation_identity(invocation)}".encode()).hexdigest()


@dataclass(frozen=True)
class PolicyDecision:
    invocation: Invocation
    revision: int
    fingerprint: str


class ExecutionPolicy:
    def __init__(self, config: PolicyConfig | JsonObject) -> None:
        config = config if isinstance(config, PolicyConfig) else PolicyConfig.model_validate(config)
        self.default = config.default
        self.rules = {(rule.service_id, rule.operation): rule.mode for rule in config.rules}
        self.revision = 0
        self.pending: dict[str, tuple[Invocation, int]] = {}
        self.seen: dict[str, str] = {}
        self.approved: set[str] = set()
        self.denied: set[str] = set()

    def mode(self, invocation: Invocation) -> Mode:
        if invocation.service_id == SYSTEM_SERVICE:
            return PolicyMode.ALLOW
        wildcard = self.rules.get((invocation.service_id, WILDCARD), self.default)
        return self.rules.get((invocation.service_id, invocation.operation), wildcard)

    def request(self, invocation: Invocation) -> PolicyOutcome:
        identity = _invocation_identity(invocation)
        prior_identity = self.seen.get(invocation.id)
        if prior_identity is not None and prior_identity != identity:
            return PolicyOutcome.DENIED
        self.seen[invocation.id] = identity
        mode = self.mode(invocation)
        if mode == PolicyMode.DENY:
            return PolicyOutcome.DENIED
        if mode == PolicyMode.ALLOW:
            return PolicyOutcome.ALLOWED
        key = _approval_fingerprint(invocation, self.revision)
        if key in self.denied:
            return PolicyOutcome.DENIED
        if key in self.approved:
            return PolicyOutcome.ALLOWED
        self.pending[invocation.id] = (invocation, self.revision)
        return PolicyOutcome.PENDING

    def decide(self, invocation_id: str, approve: bool) -> PolicyDecision:
        pending = self.pending.get(invocation_id)
        if pending is None:
            raise ScenarioError(f"operator decision has no pending invocation: {invocation_id}")
        invocation, revision = pending
        if revision != self.revision or self.mode(invocation) != PolicyMode.APPROVAL:
            raise ScenarioError(f"operator decision references stale policy: {invocation_id}")
        key = _approval_fingerprint(invocation, self.revision)
        (self.approved if approve else self.denied).add(key)
        self.pending.pop(invocation_id, None)
        return PolicyDecision(invocation, self.revision, key)

    def consume(self, invocation: Invocation) -> None:
        """An approval covers the one invocation it answered and is used up by its attempt. A
        retry, even of an identical call after a failure, is a new request."""
        self.pending.pop(invocation.id, None)
        self.approved.discard(_approval_fingerprint(invocation, self.revision))

    def retire(self, invocation_ids: list[str]) -> None:
        for invocation_id in invocation_ids:
            self.pending.pop(invocation_id, None)

    def change(self, service_id: str, operation: str, mode: Mode) -> None:
        self.rules[(service_id, operation)] = mode
        self.revision += 1
        self.pending.clear()
        self.approved.clear()
        self.denied.clear()

    def awaiting_decision(self, invocation_id: str) -> bool:
        pending = self.pending.get(invocation_id)
        if pending is None:
            return False
        invocation, revision = pending
        return (
            revision == self.revision
            and self.mode(invocation) == PolicyMode.APPROVAL
            and _approval_fingerprint(invocation, revision) not in self.approved
        )
