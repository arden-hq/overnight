from pathlib import Path
from typing import Literal

from feather_harness import CompactionConfig
from feather_harness.production import production_compaction
from feather_harness.subagents import SubagentConfig
from pydantic import Field, model_validator

from overnight.domain.contracts import Contract


class NativeOptions(Contract):
    profile: Literal["world", "train", "prod"] = "world"
    image_tools: bool = False
    web_tools: bool = False
    skills: Path | None = Field(default=None, strict=False)
    subagents: SubagentConfig | None = None
    compaction: CompactionConfig = Field(default_factory=lambda: CompactionConfig(max_tokens=2048))

    @property
    def effective_compaction(self) -> CompactionConfig:
        if self.profile == "prod":
            return production_compaction(self.compaction)
        return self.compaction.model_copy(update={"enabled": bool(self.compaction.enabled)})

    @model_validator(mode="after")
    def require_native_profile(self):
        if self.profile == "world" and (
            self.image_tools or self.web_tools or self.skills or self.subagents
        ):
            raise ValueError("native tools, skills and subagents require train/prod profile")
        if self.subagents is not None and self.profile != "prod":
            raise ValueError("Feather subagents require the native prod profile")
        return self
