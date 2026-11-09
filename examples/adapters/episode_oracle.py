import sys
from pathlib import Path

from pydantic import TypeAdapter

from overnight.domain.contracts import Action, JsonObject, WaitAction, action_contract

plans = TypeAdapter(dict[str, list[Action]]).validate_json(Path(sys.argv[1]).read_bytes())
positions: dict[str, int] = {}
observation_contract = TypeAdapter(JsonObject)

for line in sys.stdin:
    observation = observation_contract.validate_json(line)
    task = observation.get("task")
    if not isinstance(task, dict):
        raise ValueError("episode oracle requires an explicit task ID")
    task_id = task.get("id")
    if not isinstance(task_id, str):
        raise ValueError("episode oracle requires a string task ID")
    position = positions.get(task_id, 0)
    if task_id not in plans or position >= len(plans[task_id]):
        raise ValueError(f"no remaining action for task {task_id}")
    action = plans[task_id][position]
    now = observation.get("time")
    while isinstance(action, WaitAction) and action.until is not None:
        if not isinstance(now, int) or now < action.until:
            break
        position += 1
        if position >= len(plans[task_id]):
            raise ValueError(f"plan for task {task_id} ends with a wait")
        action = plans[task_id][position]
    positions[task_id] = position if isinstance(action, WaitAction) else position + 1
    sys.stdout.write(action_contract.dump_json(action).decode() + "\n")
    sys.stdout.flush()
