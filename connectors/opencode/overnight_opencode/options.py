from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator

from overnight.adapters.provider import Provider
from overnight.adapters.provider_relay import RequestBudget
from overnight.domain.contracts import Contract

type BuiltinPermission = Literal["read", "edit", "shell", "task", "subagent"]


class NativeProfile(Contract):
    allow: list[BuiltinPermission] = Field(default_factory=list)
    skills: list[Path] = Field(default_factory=list)
    full_native_tools: bool = True


class OpenCodeOptions(RequestBudget):
    runtime: Path = Field(strict=False)
    provider: Provider = "openrouter"
    model: str = "overnight/openai/gpt-6-luna"
    input_usd_per_million: float = Field(default=0.2, gt=0)
    output_usd_per_million: float = Field(default=0.75, gt=0)
    native: NativeProfile = Field(default_factory=NativeProfile)

    @field_validator("model")
    @classmethod
    def require_provider_selector(cls, value: str) -> str:
        if "/" not in value:
            raise ValueError("model must use OpenCode's provider/model selector")
        return value

    @property
    def upstream_model(self) -> str:
        """The model ID OpenCode sends to the provider, after its provider selector."""
        return self.model.split("/", 1)[1]
