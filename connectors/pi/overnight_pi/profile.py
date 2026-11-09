from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field, field_validator

from overnight.domain.contracts import Contract

type BuiltinTool = Literal["read", "bash", "powershell", "edit", "write", "grep", "find", "ls"]
type ThinkingLevel = Literal["off", "minimal", "low", "medium", "high", "xhigh", "max"]
type ResourcePath = Annotated[Path, Field(strict=False)]


class NativeProfile(Contract):
    builtin_tools: list[BuiltinTool] = Field(default_factory=list)
    extensions: list[ResourcePath] = Field(default_factory=list)
    skills: list[ResourcePath] = Field(default_factory=list)
    prompt_templates: list[ResourcePath] = Field(default_factory=list)
    thinking: ThinkingLevel = "off"
    compaction: bool = False

    @field_validator("extensions", "skills", "prompt_templates")
    @classmethod
    def require_local_resources(cls, paths: list[Path]) -> list[Path]:
        resources = [path.resolve() for path in paths]
        for resource in resources:
            if not resource.exists():
                raise ValueError(f"native resource does not exist: {resource}")
        return resources

    def arguments(self) -> list[str]:
        arguments = [
            "--no-extensions",
            "--no-skills",
            "--no-prompt-templates",
            "--no-context-files",
            "--no-approve",
            "--offline",
            "--thinking",
            self.thinking,
        ]
        if not self.builtin_tools:
            arguments.append("--no-builtin-tools")
        for flag, paths in (
            ("--extension", self.extensions),
            ("--skill", self.skills),
            ("--prompt-template", self.prompt_templates),
        ):
            for path in paths:
                arguments.extend((flag, str(path)))
        return arguments
