from collections.abc import Mapping
from typing import Self

from pydantic import Field, JsonValue, model_validator

from overnight.domain.contracts import JsonObject, NonemptySortedNames, TrimmedText
from overnight.domain.models import Actor, Context
from overnight.domain.vocabulary import NotificationKind
from overnight.services.common import find_row, next_id, validate_participants
from overnight.services.operations import NoArguments, OperationSpec, Parameters, Service
from overnight.services.state import State, distinct


class ChatMessage(State):
    id: TrimmedText
    sender: TrimmedText = Field(alias="from")
    body: TrimmedText
    date: int


class Conversation(State):
    id: TrimmedText
    participants: NonemptySortedNames
    messages: list[ChatMessage] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_message_senders(self) -> Self:
        if any(message.sender not in self.participants for message in self.messages):
            raise ValueError("Chat sender must be a participant")
        return self


class ChatState(State):
    conversations: list[Conversation] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_unique_ids(self) -> Self:
        distinct([row.id for row in self.conversations], "conversation")
        distinct(
            [message.id for row in self.conversations for message in row.messages], "chat message"
        )
        return self


class ChatRead(Parameters):
    conversation_id: TrimmedText


class ChatSend(ChatRead):
    body: TrimmedText


class ChatStart(Parameters):
    participants: NonemptySortedNames
    body: TrimmedText


class Chat(Service[ChatState]):
    def __init__(self) -> None:
        super().__init__(
            ChatState,
            [
                OperationSpec(NoArguments, self.list_conversations, read_only=True),
                OperationSpec(ChatRead, self.read_messages, read_only=True),
                OperationSpec(ChatStart, self.start_conversation),
                OperationSpec(ChatSend, self.send_message),
            ],
        )

    def validate_actors(self, state: ChatState, actors: Mapping[str, Actor]) -> None:
        for row in state.conversations:
            validate_participants(row.participants, actors, "conversation participants")

    def visible(self, state: ChatState, actor: Actor, context: Context) -> JsonObject:
        return {
            "as_of": context.now,
            "conversations": [
                row.view_object() for row in state.conversations if actor.id in row.participants
            ],
        }

    def _find_conversation(
        self, state: ChatState, conversation_id: str, actor_id: str
    ) -> Conversation:
        return find_row(
            state.conversations,
            lambda row: row.id == conversation_id and actor_id in row.participants,
            "conversation",
        )

    def _create_message(self, state: ChatState, body: str, context: Context) -> ChatMessage:
        messages = [message for row in state.conversations for message in row.messages]
        return ChatMessage(
            id=next_id("msg", messages), sender=context.actor.id, body=body, date=context.now
        )

    def list_conversations(
        self, state: ChatState, args: NoArguments, context: Context
    ) -> JsonObject:
        """List conversations the actor participates in."""
        conversations: list[JsonValue] = []
        for conversation in state.conversations:
            if context.actor.id not in conversation.participants:
                continue
            last_message = None
            if conversation.messages:
                last_message = conversation.messages[-1].view_object()
            summary = conversation.model_dump(mode="json", include={"id", "participants"})
            summary["last_message"] = last_message
            conversations.append(summary)
        return {"as_of": context.now, "conversations": conversations}

    def read_messages(self, state: ChatState, args: ChatRead, context: Context) -> JsonObject:
        """Read one participating conversation."""
        row = self._find_conversation(state, args.conversation_id, context.actor.id)
        return {
            "conversation_id": row.id,
            "as_of": context.now,
            "messages": [message.view_object() for message in row.messages],
        }

    def send_message(self, state: ChatState, args: ChatSend, context: Context) -> JsonObject:
        """Send to a participating conversation."""
        row = self._find_conversation(state, args.conversation_id, context.actor.id)
        message = self._create_message(state, args.body, context)
        row.messages = [*row.messages, message]
        _notify_participants(row, context)
        return {"id": message.id, "conversation_id": row.id, "sent": True}

    def start_conversation(self, state: ChatState, args: ChatStart, context: Context) -> JsonObject:
        """Start a conversation with known world actors."""
        validate_participants(args.participants, context.actors, "participants")
        if context.actor.id in args.participants:
            raise ValueError("Do not repeat the sender in participants")
        message = self._create_message(state, args.body, context)
        row = Conversation(
            id=next_id("conversation", state.conversations),
            participants=[context.actor.id, *args.participants],
            messages=[message],
        )
        state.conversations = [*state.conversations, row]
        _notify_participants(row, context)
        return {"conversation_id": row.id, "message_id": message.id}


def _notify_participants(row: Conversation, context: Context) -> None:
    context.notify(
        row.participants,
        NotificationKind.CHAT,
        row.id,
        f"New chat message from {context.actor.id} in conversation {row.id}",
    )
