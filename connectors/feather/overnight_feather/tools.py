from collections.abc import Callable
from typing import TYPE_CHECKING

from feather_harness import Tool, ToolResult, ToolSpec
from mcp import ClientSession
from pydantic import Field, TypeAdapter

from overnight.domain.contracts import Contract, JsonObject
from overnight.execution.tool_server import NATIVE_CONTEXT_KEY

if TYPE_CHECKING:
    from overnight_feather.session import Chat

arguments_contract = TypeAdapter(JsonObject)


class Question(Contract):
    message: str = Field(min_length=1)


class Wake(Contract):
    time: int = Field(ge=0)
    message: str = Field(min_length=1)


def world_tool(chat: "Chat", service: str, operation, session: ClientSession) -> Tool:
    name = f"{service}__{operation.name}"

    async def invoke(raw: str) -> ToolResult:
        identity = chat.identity(name)
        try:
            arguments = arguments_contract.validate_json(raw)
        except ValueError as error:
            return ToolResult(content=str(error), error="invalid_arguments")
        result = await session.call_tool(
            operation.name,
            arguments,
            meta={NATIVE_CONTEXT_KEY: identity.model_dump(mode="json")},
        )
        text = "\n".join(block.text for block in result.content if block.type == "text")
        return ToolResult(content=text, error="tool_error" if result.isError else None)

    return Tool(
        spec=ToolSpec(
            name=name, description=operation.description or "", parameters=operation.inputSchema
        ),
        invoke=invoke,
    )


def wake_tool(chat: "Chat", record: Callable[[JsonObject], None]) -> Tool:
    async def invoke(raw: str) -> ToolResult:
        identity = chat.identity("schedule_wake")
        try:
            wake = Wake.model_validate_json(raw)
            if wake.time <= chat.now:
                raise ValueError("wake time must be after the current simulated time")
        except ValueError as error:
            return ToolResult(content=str(error), error="invalid_arguments")
        chat.wake = wake
        record(
            {
                "kind": "controller_wake_scheduled",
                **identity.model_dump(mode="json"),
                **wake.model_dump(mode="json"),
            }
        )
        return ToolResult(content=f"Connector wake scheduled at {wake.time}. End this turn.")

    return Tool(
        spec=ToolSpec(
            name="schedule_wake",
            description=(
                "Schedule an Overnight connector continuation at an absolute simulated time. "
                "Keeps this chat's native Feather history. This is not a native Feather scheduler."
            ),
            parameters=Wake.model_json_schema(),
        ),
        invoke=invoke,
    )


def question_tool(chat: "Chat", record: Callable[[JsonObject], None]) -> Tool:
    async def invoke(raw: str) -> ToolResult:
        identity = chat.identity("ask_operator")
        try:
            question = Question.model_validate_json(raw)
        except ValueError as error:
            return ToolResult(content=str(error), error="invalid_arguments")
        chat.question = question.message
        record(
            {
                "kind": "controller_question",
                **identity.model_dump(mode="json"),
                "message": question.message,
            }
        )
        return ToolResult(content="Question sent to the operator; wait for their answer.")

    return Tool(
        spec=ToolSpec(
            name="ask_operator",
            description=(
                "Ask for missing information or a decision, then wait for the operator message. "
                "Do not request world-tool approval here: calling that tool requests its scoped approval. "
                "Do not repeat an answered question. Connector-owned continuation."
            ),
            parameters=Question.model_json_schema(),
        ),
        invoke=invoke,
    )
