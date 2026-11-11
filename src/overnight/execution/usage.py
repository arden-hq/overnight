import math
from collections.abc import Iterable, Sequence
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, ValidationError


class ProcessUsage(BaseModel):
    """Totals for one agent process or a whole run."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    calls: int | None = Field(default=None, ge=0)
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    cost_usd: float | None = Field(default=None, ge=0, allow_inf_nan=False)

    def plus(self, other: "ProcessUsage") -> "ProcessUsage":
        return ProcessUsage(
            calls=_sum(self.calls, other.calls),
            input_tokens=_sum(self.input_tokens, other.input_tokens),
            output_tokens=_sum(self.output_tokens, other.output_tokens),
            cost_usd=_sum(self.cost_usd, other.cost_usd),
        )


def _sum[T: (int, float)](left: T | None, right: T | None) -> T | None:
    if left is None or right is None:
        return None
    total = left + right
    return None if total == math.inf else total


def usage_path(provenance_path: Path) -> Path:
    return provenance_path.with_suffix(".usage.json")


def capture_path(provenance_path: Path) -> Path:
    """Where a capturing process stores its model calls, beside its provenance sidecar."""
    return provenance_path.with_suffix(".capture.jsonl")


UNMEASURED = ProcessUsage()
NO_CALLS = ProcessUsage(calls=0, input_tokens=0, output_tokens=0, cost_usd=0.0)


def readable(**values: object) -> dict[str, object]:
    kept: dict[str, object] = {}
    for name, value in values.items():
        try:
            ProcessUsage.model_validate({name: value})
        except ValidationError:
            continue
        kept[name] = value
    return kept


def total_usage(usages: Iterable[ProcessUsage]) -> ProcessUsage:
    usages = list(usages)
    if not usages:
        return UNMEASURED
    total = usages[0]
    for usage in usages[1:]:
        total = total.plus(usage)
    return total


def read_usage(provenance_paths: Sequence[Path]) -> ProcessUsage:
    usages: list[ProcessUsage] = []
    for path in provenance_paths:
        try:
            usages.append(ProcessUsage.model_validate_json(usage_path(path).read_text()))
        except OSError, ValueError:
            usages.append(UNMEASURED)
    return total_usage(usages)
