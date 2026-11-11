"""Overnight scenario families as tinker-cookbook RL environments.

`OvernightDatasetBuilder` is the `dataset_builder` of a cookbook `rl.train.Config`;
`python -m overnight_tinker.train` runs the cookbook's loop with it.
"""

from overnight_tinker.cases import Case, family_cases
from overnight_tinker.dataset import OvernightDataset, OvernightDatasetBuilder
from overnight_tinker.env import (
    EpisodeConfig,
    OvernightEnv,
    OvernightEnvGroupBuilder,
    OvernightError,
)

__all__ = [
    "Case",
    "EpisodeConfig",
    "OvernightDataset",
    "OvernightDatasetBuilder",
    "OvernightEnv",
    "OvernightEnvGroupBuilder",
    "OvernightError",
    "family_cases",
]
