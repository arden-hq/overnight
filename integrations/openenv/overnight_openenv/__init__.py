"""Overnight episodes as OpenEnv loop-owning sessions for TRL's `HarnessAsyncGRPOTrainer`.

factory = functools.partial(OvernightSessionFactory, family, llm_url=..., model=..., ...)
rows = factory().prompt_rows()
HarnessAsyncGRPOTrainer(model, harness_session_factory=factory, train_dataset=Dataset.from_list(rows), ...)
"""

from overnight_openenv.cases import Case
from overnight_openenv.session import (
    CaptureProxy,
    OvernightError,
    OvernightSession,
    OvernightSessionFactory,
)

__all__ = [
    "CaptureProxy",
    "Case",
    "OvernightError",
    "OvernightSession",
    "OvernightSessionFactory",
]
