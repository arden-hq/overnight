COMMAND_TIMEOUT_SECONDS = 30
MAX_EXCHANGE_TIMEOUT_SECONDS = 300
PERSISTENT_TIMEOUT_SECONDS = MAX_EXCHANGE_TIMEOUT_SECONDS
GRACEFUL_EXIT_SECONDS = 10
TERMINATE_WAIT_SECONDS = 5
# Shell conventions: the command could not run at all.
UNRUNNABLE_EXIT_CODES = frozenset({126, 127})
USAGE_PATH_VARIABLE = "OVERNIGHT_USAGE_PATH"
# Set only for a run that captures model tokens for training; never for an evaluation.
CAPTURE_PATH_VARIABLE = "OVERNIGHT_CAPTURE_PATH"
# A fresh private folder of the agent process's own, unique to this process in this run: a
# connector keeps its native evidence and working files there, so an agent document that runs in
# many episodes never shares a path between them.
AGENT_DIRECTORY_VARIABLE = "OVERNIGHT_AGENT_DIRECTORY"
