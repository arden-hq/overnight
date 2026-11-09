from enum import StrEnum

WILDCARD = "*"
SYSTEM_SERVICE = "system"


class ActorRole(StrEnum):
    OPERATOR = "operator"
    AGENT = "agent"
    PERSON = "person"
    SYSTEM = "system"


class ResultStatus(StrEnum):
    SKIPPED = "skipped"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    DENIED = "denied"
    WAITING = "waiting"


class NotificationKind(StrEnum):
    MAIL = "mail_received"
    CHAT = "chat_message"
    CALENDAR = "calendar_changed"
    RECORD = "record_changed"
    ORDER = "order_changed"
    REMINDER = "reminder_due"
    OPERATOR = "operator_message"


class RunStatus(StrEnum):
    RUNNING = "running"
    DONE = "done"
    HORIZON = "horizon"
    MAX_STEPS = "max_steps"
    INVALID = "invalid"


class FailureKind(StrEnum):
    AGENT = "agent"
    INFRASTRUCTURE = "infrastructure"


class PolicyMode(StrEnum):
    DENY = "deny"
    ALLOW = "allow"
    APPROVAL = "approval"


class PolicyOutcome(StrEnum):
    ALLOWED = "allowed"
    PENDING = "pending"
    APPROVED = "approved"
    DENIED = "denied"


class Verdict(StrEnum):
    PASSED = "passed"
    FAILED = "failed"
    UNKNOWN = "unknown"
    INVALID = "invalid"


class ApprovalDecision(StrEnum):
    APPROVE = "approve"
    DENY = "deny"
