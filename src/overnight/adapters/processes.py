import os
import signal
import subprocess
import time
from collections.abc import Sequence
from dataclasses import dataclass, field

from overnight.domain.transport import GRACEFUL_EXIT_SECONDS, TERMINATE_WAIT_SECONDS

STOP_GRACE_SECONDS = float(GRACEFUL_EXIT_SECONDS + TERMINATE_WAIT_SECONDS + 5)


@dataclass
class ProcessTree:
    process: subprocess.Popen[bytes]
    agent_groups: set[int] = field(default_factory=set)

    def observe(self, listing: str | None = None) -> None:
        if self.process.poll() is None:
            self.agent_groups = _descendant_groups(
                self.process.pid, process_listing() if listing is None else listing
            )

    def send(self, sig: signal.Signals, listing: str | None = None) -> None:
        self.observe(listing)
        groups = {self.process.pid}
        if sig == signal.SIGKILL:
            groups |= self.agent_groups
        for group in groups:
            _signal_group(group, sig)

    def stop(self, grace: float = STOP_GRACE_SECONDS) -> None:
        stop_trees([self], grace)


def stop_trees(trees: Sequence[ProcessTree], grace: float = STOP_GRACE_SECONDS) -> None:
    listing = process_listing()
    for tree in trees:
        tree.send(signal.SIGTERM, listing)
    deadline = time.monotonic() + grace
    for tree in trees:
        try:
            tree.process.wait(max(0.0, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            pass
    listing = process_listing()
    for tree in trees:
        tree.send(signal.SIGKILL, listing)
        tree.process.wait()


def process_listing() -> str:
    try:
        return subprocess.run(
            ["ps", "-axo", "pid=,ppid=,pgid="],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        ).stdout
    except OSError, subprocess.SubprocessError:
        return ""


def _descendant_groups(pid: int, listing: str) -> set[int]:
    children: dict[int, list[tuple[int, int]]] = {}
    for line in listing.splitlines():
        fields = line.split()
        if len(fields) == 3 and all(item.isdigit() for item in fields):
            child, parent, group = (int(item) for item in fields)
            children.setdefault(parent, []).append((child, group))
    groups: set[int] = set()
    stack = [pid]
    while stack:
        for child, group in children.get(stack.pop(), []):
            groups.add(group)
            stack.append(child)
    return groups - {0, 1, os.getpgrp()}


def _signal_group(group: int, sig: signal.Signals) -> None:
    try:
        os.killpg(group, sig)
    except ProcessLookupError, PermissionError:
        pass
