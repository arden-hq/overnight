import hashlib
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass

from overnight.domain.contracts import Actor, Notification
from overnight.domain.models import PendingWait
from overnight.domain.vocabulary import ActorRole, NotificationKind

type Inbox = tuple[str, str | None]


def notification_id(cause: str, recipient: str, chat_id: str | None = None) -> str:
    key = f"{cause}\0{recipient}" if chat_id is None else f"{cause}\0{recipient}\0{chat_id}"
    return "n-" + hashlib.sha256(key.encode()).hexdigest()[:16]


@dataclass(frozen=True)
class Posted:
    recipient: str
    cause: str
    notification: Notification


@dataclass(frozen=True)
class Settled:
    wait: PendingWait
    inbox: Inbox
    notifications: tuple[Notification, ...]

    @property
    def timed_out(self) -> bool:
        return not self.notifications


class NotificationLog:
    def __init__(self, actors: Mapping[str, Actor], can_see: Callable[[Actor, str], bool]) -> None:
        self._actors = actors
        self._can_see = can_see
        self._log: list[Posted] = []
        self._ids: dict[str, Posted] = {}
        self._floors: dict[Inbox, int] = {}
        self._delivered: dict[Inbox, set[str]] = {}
        self._waits: dict[str, tuple[PendingWait, Inbox]] = {}

    def post(
        self,
        *,
        time: int,
        cause: str,
        invoker: str | None,
        recipients: Iterable[str],
        kind: NotificationKind,
        service_id: str | None,
        ref: str | None,
        summary: str,
        chat_id: str | None = None,
        chats: Mapping[str, str] | None = None,
        invoker_chat: str | None = None,
    ) -> list[Posted]:
        posted: list[Posted] = []
        for recipient in dict.fromkeys(recipients):
            actor = self._actors.get(recipient)
            if actor is None or actor.role != ActorRole.AGENT:
                continue
            if service_id is not None and not self._can_see(actor, service_id):
                continue
            if recipient != invoker:
                targets = [
                    (notification_id(cause, recipient), (chats or {}).get(recipient, chat_id))
                ]
            elif invoker_chat is None:
                continue
            else:
                targets = [
                    (notification_id(cause, recipient, chat), chat)
                    for chat in self._other_chats(
                        recipient, invoker_chat, (chats or {}).get(recipient)
                    )
                ]
            for id_, target in targets:
                notification = Notification(
                    id=id_,
                    time=time,
                    kind=kind,
                    service_id=service_id,
                    ref=ref,
                    summary=summary,
                    chat_id=target,
                )
                prior = self._ids.get(notification.id)
                if prior is not None:
                    if prior.notification.model_dump(exclude={"time"}) != notification.model_dump(
                        exclude={"time"}
                    ):
                        raise ValueError(
                            f"notification {notification.id} was posted with other content"
                        )
                    continue
                entry = Posted(recipient, cause, notification)
                self._log.append(entry)
                self._ids[notification.id] = entry
                posted.append(entry)
        return posted

    def _other_chats(self, actor_id: str, invoker_chat: str, thread_chat: str | None) -> list[str]:
        if thread_chat is not None:
            return [] if thread_chat == invoker_chat else [thread_chat]
        return [
            chat
            for actor, chat in self._floors
            if actor == actor_id and chat is not None and chat != invoker_chat
        ]

    def open(self, actor_id: str, chat_id: str | None) -> None:
        inbox = (actor_id, chat_id)
        self._floors[inbox] = len(self._log)
        self._delivered.setdefault(inbox, set())

    def resolve(self, actor_id: str, chat_id: str | None) -> Inbox:
        for inbox in ((actor_id, chat_id), (actor_id, None)):
            if inbox in self._floors:
                return inbox
        raise ValueError(f"no open notification inbox for {actor_id}")

    def undelivered(self, actor_id: str, chat_id: str | None) -> list[Notification]:
        inbox = (actor_id, chat_id)
        floor = self._floors.get(inbox)
        if floor is None:
            return []
        delivered = self._delivered[inbox]
        return [
            entry.notification
            for entry in self._log[floor:]
            if entry.recipient == actor_id
            and (chat_id is None or entry.notification.chat_id in {None, chat_id})
            and entry.notification.id not in delivered
        ]

    def deliver(self, actor_id: str, chat_id: str | None, ids: Iterable[str]) -> list[str]:
        inbox = (actor_id, chat_id)
        offered = {notification.id for notification in self.undelivered(actor_id, chat_id)}
        delivered = self._delivered.setdefault(inbox, set())
        fresh: list[str] = []
        for notification_id_ in dict.fromkeys(ids):
            if notification_id_ in delivered:
                continue
            if notification_id_ not in offered:
                raise ValueError(f"notification {notification_id_} is not addressed to this inbox")
            delivered.add(notification_id_)
            fresh.append(notification_id_)
        return fresh

    def begin_wait(self, wait: PendingWait, inbox: Inbox) -> None:
        if wait.invocation.id in self._waits:
            raise ValueError("wait is already pending")
        self._waits[wait.invocation.id] = (wait, inbox)

    def settle(self, now: int) -> list[Settled]:
        settled: list[Settled] = []
        for invocation_id, (wait, inbox) in list(self._waits.items()):
            notifications = tuple(self.undelivered(*inbox))
            if not notifications and now < wait.until:
                continue
            self._delivered[inbox].update(notification.id for notification in notifications)
            del self._waits[invocation_id]
            settled.append(Settled(wait, inbox, notifications))
        return settled

    def expire(self, actor_id: str, chat_id: str | None) -> list[Settled]:
        settled: list[Settled] = []
        for invocation_id, (wait, inbox) in list(self._waits.items()):
            if inbox == (actor_id, chat_id):
                del self._waits[invocation_id]
                settled.append(Settled(wait, inbox, ()))
        return settled

    def next_until(self) -> int | None:
        return min((wait.until for wait, _ in self._waits.values()), default=None)
