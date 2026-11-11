"""Train on a generated Overnight family with tinker-cookbook's RL loop.

    python -m overnight_tinker.train family=DIR agent=agent.json operator_model=operator.json \
        model_name=Qwen/Qwen3-8B log_path=DIR
"""

import asyncio

import chz
from tinker_cookbook import cli_utils
from tinker_cookbook.rl import train
from tinker_cookbook.rl.rollout_strategy import MinViableGroup

from overnight_tinker.dataset import OvernightDatasetBuilder


@chz.chz
class CLIConfig:
    family: str
    agent: str
    operator_model: str
    log_path: str
    model_name: str = "Qwen/Qwen3-8B"
    renderer_name: str | None = None
    held_out: tuple[str, ...] = ()
    group_size: int = 4
    groups_per_batch: int = 8
    learning_rate: float = 1e-5
    lora_rank: int = 32
    max_tokens: int = 2048
    temperature: float = 1.0
    rollout_timeout_seconds: float | None = 1800
    sampling_turn_timeout_seconds: float | None = None
    pass_env: tuple[str, ...] = ()
    root: str | None = None
    keep: bool = False
    max_steps: int | None = None
    save_every: int = 10
    wandb_project: str | None = None
    behavior_if_log_dir_exists: cli_utils.LogdirBehavior = "ask"


def config(cli: CLIConfig) -> train.Config:
    dataset = OvernightDatasetBuilder(
        family=cli.family,
        agent=cli.agent,
        operator_model=cli.operator_model,
        model_name=cli.model_name,
        renderer_name=cli.renderer_name,
        held_out=cli.held_out,
        group_size=cli.group_size,
        groups_per_batch=cli.groups_per_batch,
        rollout_timeout_seconds=cli.rollout_timeout_seconds,
        sampling_turn_timeout_seconds=cli.sampling_turn_timeout_seconds,
        pass_env=cli.pass_env,
        root=cli.root,
        keep=cli.keep,
    )
    return train.Config(
        learning_rate=cli.learning_rate,
        dataset_builder=dataset,
        model_name=cli.model_name,
        recipe_name="overnight_tinker",
        renderer_name=cli.renderer_name,
        max_tokens=cli.max_tokens,
        temperature=cli.temperature,
        lora_rank=cli.lora_rank,
        log_path=cli.log_path,
        save_every=cli.save_every,
        eval_every=0,
        wandb_project=cli.wandb_project,
        max_steps=cli.max_steps,
        # An episode Overnight could not grade raises; this drops it and keeps the group while
        # most of it survived, instead of FailFast stopping the run.
        rollout_error_tolerance=MinViableGroup(),
    )


async def main(cli: CLIConfig) -> None:
    cli_utils.check_log_dir(cli.log_path, behavior_if_exists=cli.behavior_if_log_dir_exists)
    await train.main(config(cli))


if __name__ == "__main__":
    asyncio.run(main(chz.entrypoint(CLIConfig)))
