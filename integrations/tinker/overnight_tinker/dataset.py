"""A generated scenario family as a tinker-cookbook RL dataset."""

import json
import os
from collections.abc import Sequence
from pathlib import Path

import chz
from tinker_cookbook.rl.types import EnvGroupBuilder, RLDataset, RLDatasetBuilder

from overnight_tinker.cases import Case, family_cases
from overnight_tinker.env import EpisodeConfig, OvernightEnvGroupBuilder, operator_key


class OvernightDataset(RLDataset):
    """Batches of `groups_per_batch` cases, one group of `group_size` episodes per case."""

    def __init__(
        self, config: EpisodeConfig, cases: list[Case], group_size: int, groups_per_batch: int
    ) -> None:
        self.builders = [OvernightEnvGroupBuilder(config, case, group_size) for case in cases]
        self.groups_per_batch = groups_per_batch

    def __len__(self) -> int:
        return -(-len(self.builders) // self.groups_per_batch)

    def get_batch(self, index: int) -> Sequence[EnvGroupBuilder]:
        start = index * self.groups_per_batch
        return self.builders[start : start + self.groups_per_batch]


def _json(path: str) -> dict:
    document = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise ValueError(f"{path} is not a JSON object")
    return document


@chz.chz
class OvernightDatasetBuilder(RLDatasetBuilder):
    family: str
    """A folder written by `overnight generate ... --out DIR` (it holds `family.json`)."""
    operator_model: str
    """A JSON Overnight operator model (`provider: {base_url, api_key_env}`, `model`, bounds) that
    answers persona cases. Required: the trained policy never plays the operator."""
    model_name: str
    agent: str | None = None
    """A JSON Overnight agent document that names `{policy.base_url}`, `{policy.api_key_env}`
    and `{policy.model}`. None keeps each case's own agent, which calls no model."""
    held_out: tuple[str, ...] = ()
    """Hand-written scenario and suite folders the family must not overlap. Default: the
    repository's `examples`, as `overnight.families.split` defaults."""
    python: str = chz.field(default_factory=lambda: os.environ.get("OVERNIGHT_PYTHON", ""))
    renderer_name: str | None = None
    group_size: int = 4
    groups_per_batch: int = 8
    root: str | None = None
    keep: bool = False
    pass_env: tuple[str, ...] = ()
    """Variables of the trainer's environment the worker keeps beside its own allow list."""
    rollout_timeout_seconds: float | None = None
    sampling_turn_timeout_seconds: float | None = None

    def episode_config(self) -> EpisodeConfig:
        config = EpisodeConfig(
            python=self.python,
            model_name=self.model_name,
            operator_model=_json(self.operator_model),
            agent=_json(self.agent) if self.agent is not None else None,
            renderer_name=self.renderer_name,
            root=self.root,
            keep=self.keep,
            pass_env=self.pass_env,
            rollout_timeout_seconds=self.rollout_timeout_seconds,
            sampling_turn_timeout_seconds=self.sampling_turn_timeout_seconds,
        )
        operator_key(config)  # fail at load, not in the first persona episode
        return config

    async def __call__(self) -> tuple[RLDataset, RLDataset | None]:
        config = self.episode_config()
        held_out = [Path(folder) for folder in self.held_out]
        cases = family_cases(self.python, Path(self.family), held_out)
        return OvernightDataset(config, cases, self.group_size, self.groups_per_batch), None
