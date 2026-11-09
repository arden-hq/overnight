import json
from collections import deque
from dataclasses import dataclass, field

from feather_harness import ChatCompletionsModel, ModelSettings
from feather_harness.core.messages import ToolCall
from feather_harness.core.model import ModelCall
from pydantic import Field

from overnight.adapters.evidence import UsageMeter
from overnight.domain.contracts import Contract


class Pricing(Contract):
    input_usd_per_million: float = Field(default=0.75, gt=0)
    output_usd_per_million: float = Field(default=4.5, gt=0)


class IdentifiedModel(ChatCompletionsModel):
    def __init__(
        self,
        *,
        calls: deque[ToolCall],
        budget: "Budget",
        record,
        tracked_tools: frozenset[str] | None = None,
        **kwargs,
    ):
        super().__init__(**kwargs)
        self.calls = calls
        self.budget = budget
        self.record = record
        self.tracked_tools = tracked_tools

    async def complete(self, *args, **kwargs) -> ModelCall:
        if self.calls:
            raise RuntimeError("native tool-call identities were not consumed")
        reserved = self.budget.check(args[0], kwargs.get("tools", ()), kwargs["settings"])
        self.budget.meter.begin()
        try:
            result = await super().complete(*args, **kwargs)
        except BaseException:
            self.budget.meter.add_unmeasured()
            self.budget.reserved -= reserved
            self.budget.spent += reserved
            raise
        self.budget.reserved -= reserved
        self.record(
            {
                "kind": "provider_usage",
                "model": self.model,
                "usage": result.usage.model_dump(mode="json") if result.usage else None,
            }
        )
        self.budget.charge(result)
        self.record(
            {
                "kind": "native_model_calls",
                "calls": [call.model_dump(mode="json") for call in result.assistant.tool_calls],
            }
        )
        self.calls.extend(
            call
            for call in result.assistant.tool_calls
            if self.tracked_tools is None or call.name in self.tracked_tools
        )
        return result


@dataclass
class Budget:
    limit: float
    spent: float = 0.0
    reserved: float = 0.0
    pricing: Pricing = field(default_factory=Pricing)
    meter: UsageMeter = field(default_factory=UsageMeter)

    def check(self, messages, tools, settings: ModelSettings) -> float:
        wire = json.dumps([message.model_dump(mode="json") for message in messages])
        schemas = json.dumps([tool.to_wire() for tool in tools])
        prompt_bound = len((wire + schemas).encode()) + 1024 + 32 * (len(messages) + len(tools))
        assert settings.max_tokens is not None
        bound = (
            prompt_bound * self.pricing.input_usd_per_million
            + settings.max_tokens * self.pricing.output_usd_per_million
        ) / 1_000_000
        if self.spent + self.reserved + bound > self.limit:
            raise RuntimeError("OpenRouter budget cannot cover the next bounded model call")
        self.reserved += bound
        return bound

    def charge(self, result: ModelCall) -> None:
        usage = result.usage
        self.meter.add(
            usage.prompt_tokens if usage else None,
            usage.completion_tokens if usage else None,
            usage.cost_usd if usage else None,
        )
        if result.usage is None or result.usage.cost_usd is None:
            self.spent = self.limit
            raise RuntimeError("OpenRouter omitted cost accounting; refusing another model call")
        self.spent += result.usage.cost_usd
        if self.spent > self.limit:
            raise RuntimeError("OpenRouter reported cost exceeds the reserved budget")
