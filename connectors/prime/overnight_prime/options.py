from pathlib import Path
from typing import Annotated

from pydantic import Field, model_validator

from overnight.adapters.provider import Provider
from overnight.domain.contracts import Contract


class AutonomousOptions(Contract):
    gates: list[str] = Field(default_factory=list)
    gate_retries: int | None = Field(default=None, ge=1)
    gate_timeout_ms: int | None = Field(default=None, ge=1)
    max_continuations: int | None = Field(default=None, ge=1)
    max_turns: int | None = Field(default=None, ge=1)
    max_tokens: int | None = Field(default=None, ge=1)
    timeout_ms: int | None = Field(default=None, ge=1)

    def arguments(self) -> list[str]:
        arguments = [argument for gate in self.gates for argument in ("--autonomous-gate", gate)]
        for name in (
            "gate_retries",
            "gate_timeout_ms",
            "max_continuations",
            "max_turns",
            "max_tokens",
            "timeout_ms",
        ):
            value = getattr(self, name)
            if value is not None:
                arguments.extend(("--autonomous-" + name.replace("_", "-"), str(value)))
        return arguments


class PrimeOptions(Contract):
    runtime: Path = Field(strict=False)
    provider: Provider = "openrouter"
    autonomous: bool = False
    autonomy: AutonomousOptions = Field(default_factory=AutonomousOptions)
    skills: list[Annotated[Path, Field(strict=False)]] = Field(default_factory=list)
    bundled_skills: list[str] = Field(
        default_factory=lambda: [
            "rlm-heartbeat",
            "goal",
            "compact",
            "agent-message",
            "agent-observe",
            "factory",
        ]
    )
    model: str = "openai/gpt-6-luna"
    max_tokens: int = Field(default=2048, ge=1, le=4096)
    max_requests: int = Field(default=12, ge=1, le=40)
    max_request_bytes: int = Field(default=262144, ge=1024, le=1048576)
    input_usd_per_million: float = Field(gt=0)
    output_usd_per_million: float = Field(gt=0)
    max_cost_usd: float = Field(default=1, gt=0, le=5)

    @model_validator(mode="after")
    def bound_native_requests(self):
        if not self.autonomous and self.autonomy.arguments():
            raise ValueError("native autonomy configuration requires autonomous=true")
        bound = (
            self.max_requests
            * (
                (self.max_request_bytes + 4096) * self.input_usd_per_million
                + self.max_tokens * self.output_usd_per_million
            )
            / 1_000_000
        )
        if bound > self.max_cost_usd:
            raise ValueError("native request limits exceed the cost bound")
        return self
