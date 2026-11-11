"""Two seats over one Overnight episode: the trained agent and a frozen operator.

Overnight's runner owns every turn. verifiers supplies one intercepted model session per seat, as
prime-envs' `tau_common` does for Tau: the `agent` session is the policy under training, and the
`operator` session answers as a persona case's operator and is never trained.
"""

import verifiers.v1 as vf

from overnight_verifiers.harness import EPISODE_SESSIONS, OvernightHarness
from overnight_verifiers.taskset import has_persona

# Pinned, so `--env.<seat>.harness.*` options validate against Overnight's harness config.
HARNESS = {"id": "overnight-verifiers"}


class OvernightEnvConfig(vf.EnvConfig):
    agent: vf.AgentConfig = vf.AgentConfig(runtime=vf.SubprocessConfig(), harness=HARNESS)
    """The policy under training."""
    operator: vf.AgentConfig = vf.AgentConfig(runtime=vf.SubprocessConfig(), harness=HARNESS)
    """The persona operator's model, required: `model`, and `client` for another endpoint.
    verifiers would otherwise answer with the run's model, the policy being trained. The env
    checks it when it loads, not the config: `vf-run` builds a default config to read its
    options."""


class OvernightEnv(vf.Env[OvernightEnvConfig]):
    def __init__(self, config: OvernightEnvConfig) -> None:
        if config.operator.model is None:
            raise ValueError(
                "set the operator seat's model (--env.operator.model): unset, the persona "
                "operator would be the policy under training"
            )
        super().__init__(config)

    async def setup(self, agents) -> None:
        for role in ("agent", "operator"):
            agent = getattr(agents, role)
            if not isinstance(agent.harness, OvernightHarness):
                raise TypeError(f"the {role} seat needs Overnight's harness")
            if not isinstance(agent.runtime_config, vf.SubprocessConfig):
                # Overnight's runner owns isolation: it puts the agent in a container itself.
                raise TypeError(f"the {role} seat runs Overnight as a host subprocess")
        agents.operator.trainable = False

    async def run(self, task, agents) -> None:
        token = EPISODE_SESSIONS.set({})
        try:
            if has_persona(task.data.scenario):
                # The operator's session stays open for the whole episode; Overnight calls it.
                async with agents.operator.interaction(vf.Task(vf.TaskData())):
                    await agents.agent.run(task)
            else:
                await agents.agent.run(task)
        finally:
            EPISODE_SESSIONS.reset(token)
