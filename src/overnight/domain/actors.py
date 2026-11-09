from collections.abc import Sequence
from dataclasses import dataclass

from overnight.domain.contracts import Actor, ScenarioError
from overnight.domain.vocabulary import ActorRole


@dataclass(frozen=True)
class ExecutionActors:
    agent: str
    operator: str


def execution_actors(actors: Sequence[Actor]) -> ExecutionActors:
    principals: dict[str, str] = {}
    for role in (ActorRole.AGENT, ActorRole.OPERATOR):
        matches = [actor.id for actor in actors if actor.role == role]
        if len(matches) != 1:
            raise ScenarioError(f"actors must include exactly one {role} role")
        principals[role] = matches[0]
    return ExecutionActors(
        agent=principals[ActorRole.AGENT], operator=principals[ActorRole.OPERATOR]
    )
