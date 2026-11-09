from collections.abc import Mapping
from enum import StrEnum
from functools import partial
from typing import Literal, Self

from pydantic import Field, JsonValue, model_validator

from overnight.domain.contracts import (
    JsonObject,
    Names,
    NonemptyNames,
    Text,
)
from overnight.domain.models import Actor, Context
from overnight.domain.vocabulary import NotificationKind
from overnight.services.common import find_row, next_id, validate_participants
from overnight.services.operations import (
    IdArgument,
    OperationSpec,
    Parameters,
    Service,
)
from overnight.services.state import State, distinct


class MailMessage(State):
    id: Text
    sender: Text = Field(alias="from")
    to: Names
    cc: Names = Field(default_factory=list)
    subject: Text
    body: str
    date: int
    draft: bool = False
    deleted_for: Names = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_recipients(self) -> Self:
        if not self.draft and not self.to:
            raise ValueError("sent mail requires recipients")
        return self


class MailState(State):
    messages: list[MailMessage] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_unique_ids(self) -> Self:
        distinct([row.id for row in self.messages], "mail")
        return self


class MailFolder(StrEnum):
    INBOX = "inbox"
    SENT = "sent"
    DRAFTS = "drafts"


class MailList(Parameters):
    folder: Literal[MailFolder.INBOX, MailFolder.SENT, MailFolder.DRAFTS] = MailFolder.INBOX
    query: str = ""


class MailDraft(Parameters):
    to: Names = Field(default_factory=list)
    cc: Names = Field(default_factory=list)
    subject: Text
    body: str


class MailSend(MailDraft):
    to: NonemptyNames


class MailDelete(Parameters):
    ids: NonemptyNames


def _mail_visible(row: MailMessage, actor_id: str) -> bool:
    if actor_id in row.deleted_for:
        return False
    if row.draft:
        return actor_id == row.sender
    return actor_id == row.sender or actor_id in row.to or actor_id in row.cc


def _public_mail(row: MailMessage) -> JsonObject:
    return row.model_dump(mode="json", by_alias=True, exclude_unset=True, exclude={"deleted_for"})


def _deletable_inbox(message_id: str, actor_id: str, row: MailMessage) -> bool:
    return (
        row.id == message_id
        and _mail_visible(row, actor_id)
        and (actor_id in row.to or actor_id in row.cc)
    )


def _in_folder(message: MailMessage, folder: MailFolder, actor_id: str) -> bool:
    if folder == MailFolder.DRAFTS:
        return message.draft and message.sender == actor_id
    if message.draft:
        return False
    if folder == MailFolder.SENT:
        return message.sender == actor_id
    return actor_id in message.to or actor_id in message.cc


class Mail(Service[MailState]):
    def __init__(self) -> None:
        super().__init__(
            MailState,
            [
                OperationSpec(MailList, self.list_messages, read_only=True),
                OperationSpec(IdArgument, self.read_message, read_only=True),
                OperationSpec(MailSend, self.send_message),
                OperationSpec(MailDraft, self.save_draft),
                OperationSpec(MailDelete, self.delete_messages),
            ],
        )

    def validate_actors(self, state: MailState, actors: Mapping[str, Actor]) -> None:
        for row in state.messages:
            if any(actor_id not in actors for actor_id in row.deleted_for):
                raise ValueError("deleted_for has an unknown actor")
            validate_participants(row.to, actors, "recipients")
            validate_participants(row.cc, actors, "cc")

    def visible(self, state: MailState, actor: Actor, context: Context) -> JsonObject:
        return {
            "as_of": context.now,
            "messages": [
                _public_mail(row) for row in state.messages if _mail_visible(row, actor.id)
            ],
        }

    def list_messages(self, state: MailState, args: MailList, context: Context) -> JsonObject:
        """List visible mail in inbox, sent, or drafts."""
        actor_id = context.actor.id
        query = args.query.casefold()
        summaries: list[JsonValue] = []
        for message in reversed(state.messages):
            if not _mail_visible(message, actor_id):
                continue
            if not _in_folder(message, args.folder, actor_id):
                continue
            searchable_text = f"{message.sender} {message.subject} {message.body}"
            if query not in searchable_text.casefold():
                continue
            summary = message.model_dump(
                mode="json",
                by_alias=True,
                exclude_unset=True,
                include={"id", "sender", "to", "cc", "subject", "date", "draft"},
            )
            summaries.append(summary)
        return {"folder": args.folder.value, "as_of": context.now, "messages": summaries}

    def read_message(self, state: MailState, args: IdArgument, context: Context) -> JsonObject:
        """Read one visible mail by id."""
        row = find_row(
            state.messages,
            lambda row: row.id == args.id and _mail_visible(row, context.actor.id),
            "mail",
        )
        return _public_mail(row)

    def _create_message(
        self, state: MailState, args: MailDraft, context: Context, *, draft: bool
    ) -> JsonObject:
        fields = {
            "id": next_id("mail", state.messages),
            "from": context.actor.id,
            "to": args.to,
            "cc": args.cc,
            "subject": args.subject,
            "body": args.body,
            "date": context.now,
        }
        if draft:
            fields["draft"] = True
        row = MailMessage.model_validate(fields)
        state.messages = [*state.messages, row]
        return {"id": row.id, "sent": not draft}

    def send_message(self, state: MailState, args: MailSend, context: Context) -> JsonObject:
        """Send mail as the calling actor."""
        result = self._create_message(state, args, context, draft=False)
        context.notify(
            [*args.to, *args.cc],
            NotificationKind.MAIL,
            str(result["id"]),
            f"New mail from {context.actor.id}: {args.subject}",
        )
        return result

    def save_draft(self, state: MailState, args: MailDraft, context: Context) -> JsonObject:
        """Save an unsent draft as the calling actor."""
        return self._create_message(state, args, context, draft=True)

    def delete_messages(self, state: MailState, args: MailDelete, context: Context) -> JsonObject:
        """Remove inbox mail from the caller's view."""
        distinct(args.ids, "mail deletion")
        actor_id = context.actor.id
        messages_to_delete = [
            find_row(state.messages, partial(_deletable_inbox, message_id, actor_id), "inbox mail")
            for message_id in args.ids
        ]
        for message in messages_to_delete:
            message.deleted_for = [*message.deleted_for, actor_id]
        return {"deleted": len(messages_to_delete)}
