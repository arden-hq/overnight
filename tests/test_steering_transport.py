import concurrent.futures
import sys
from pathlib import Path

import pytest

from overnight.domain.contracts import HarnessRequirements
from overnight.execution.adapters import AdapterError
from overnight.execution.harness import HarnessAdapter, SteeringTarget

TIMEOUT = 10

HOST = """
import asyncio

from overnight.adapters.host import serve
from overnight.execution.harness import ActionReply, Capabilities, HarnessStatus, LifecycleReply

released = asyncio.Event()

async def observe(message):
    await released.wait()
    return ActionReply(kind="action", action={"kind": "report", "message": "done"}, status=HarnessStatus(pending=False))

async def steer(message):
    released.set()
    await asyncio.sleep(0.05)

async def lifecycle(message):
    return LifecycleReply(kind=message.kind, scope=message.scope)

async def close():
    pass

asyncio.run(serve(Capabilities(steering=True, cancel=True, reset_scope="chat"), observe, lifecycle, close, steer))
"""


def test_public_adapter_routes_concurrent_steering_to_only_the_active_turn(
    tmp_path: Path,
) -> None:
    connector = tmp_path / "connector.py"
    connector.write_text(HOST)
    requirements = HarnessRequirements(
        simulated_clock=False,
        chat_isolation=False,
        cancel=False,
        steering=True,
    )

    with HarnessAdapter([sys.executable, str(connector)], TIMEOUT, None, requirements) as adapter:
        adapter.bind_chat("chat-a")
        with concurrent.futures.ThreadPoolExecutor() as pool:
            response = pool.submit(adapter.respond, {"time": 0})
            target = adapter.wait_for_active_turn(TIMEOUT)

            wrong = adapter.steer(SteeringTarget(target.session_id, "wrong-turn"), "ignore")
            assert not wrong.accepted
            assert wrong.reason == "target_mismatch"
            assert not response.done()

            accepted = adapter.steer(
                target, "use the corrected instruction", request_id="accepted-once"
            )
            assert accepted.accepted
            assert accepted.reason is None
            assert response.result(timeout=TIMEOUT) == {"kind": "report", "message": "done"}

        duplicate = adapter.steer(target, "do not replay", request_id="accepted-once")
        assert not duplicate.accepted
        assert duplicate.reason == "duplicate"
        stale = adapter.steer(target, "too late")
        assert not stale.accepted
        assert stale.reason == "inactive"


def test_reset_retires_active_target_and_allows_a_fresh_turn(tmp_path: Path) -> None:
    connector = tmp_path / "connector.py"
    connector.write_text(HOST)
    requirements = HarnessRequirements(
        simulated_clock=False,
        chat_isolation=False,
        cancel=False,
        steering=True,
        reset_scope="chat",
    )

    with HarnessAdapter([sys.executable, str(connector)], TIMEOUT, None, requirements) as adapter:
        adapter.bind_chat("chat-a")
        with concurrent.futures.ThreadPoolExecutor() as pool:
            interrupted = pool.submit(adapter.respond, {"time": 0})
            old_target = adapter.wait_for_active_turn(TIMEOUT)
            adapter.reset()
            with pytest.raises(AdapterError, match="ended by reset"):
                interrupted.result(timeout=TIMEOUT)

            stale = adapter.steer(old_target, "must not cross reset")
            assert not stale.accepted
            assert stale.reason == "inactive"

            response = pool.submit(adapter.respond, {"time": 1})
            new_target = adapter.wait_for_active_turn(TIMEOUT)
            assert new_target.session_id != old_target.session_id
            crossed = adapter.steer(old_target, "still stale")
            assert not crossed.accepted
            assert crossed.reason == "target_mismatch"
            assert adapter.steer(new_target, "fresh turn").accepted
            assert response.result(timeout=TIMEOUT) == {"kind": "report", "message": "done"}


def test_cancel_retires_active_target_without_closing_transport(tmp_path: Path) -> None:
    connector = tmp_path / "connector.py"
    connector.write_text(HOST)
    requirements = HarnessRequirements(
        simulated_clock=False,
        chat_isolation=False,
        cancel=True,
        steering=True,
    )

    with HarnessAdapter([sys.executable, str(connector)], TIMEOUT, None, requirements) as adapter:
        with concurrent.futures.ThreadPoolExecutor() as pool:
            interrupted = pool.submit(adapter.respond, {"time": 0})
            target = adapter.wait_for_active_turn(TIMEOUT)
            adapter.cancel()
            with pytest.raises(AdapterError, match="ended by cancel"):
                interrupted.result(timeout=TIMEOUT)
            stale = adapter.steer(target, "too late")
            assert not stale.accepted
            assert stale.reason == "inactive"


def test_close_retires_active_target_and_process(tmp_path: Path) -> None:
    connector = tmp_path / "connector.py"
    connector.write_text(HOST)
    requirements = HarnessRequirements(
        simulated_clock=False,
        chat_isolation=False,
        cancel=False,
        steering=True,
    )
    adapter = HarnessAdapter([sys.executable, str(connector)], TIMEOUT, None, requirements)
    with concurrent.futures.ThreadPoolExecutor() as pool:
        interrupted = pool.submit(adapter.respond, {"time": 0})
        adapter.wait_for_active_turn(TIMEOUT)
        adapter.close()
        with pytest.raises(AdapterError, match="ended by close"):
            interrupted.result(timeout=TIMEOUT)
    assert adapter.process is None


def test_next_turn_waits_for_in_flight_steering_instead_of_failing(tmp_path: Path) -> None:
    connector = tmp_path / "connector.py"
    connector.write_text(HOST.replace("asyncio.sleep(0.05)", "asyncio.sleep(0.3)"))
    requirements = HarnessRequirements(
        simulated_clock=False, chat_isolation=False, cancel=False, steering=True
    )

    with HarnessAdapter([sys.executable, str(connector)], TIMEOUT, None, requirements) as adapter:
        with concurrent.futures.ThreadPoolExecutor() as pool:
            response = pool.submit(adapter.respond, {"time": 0})
            target = adapter.wait_for_active_turn(TIMEOUT)
            steering = pool.submit(adapter.steer, target, "late correction")
            assert response.result(timeout=TIMEOUT) == {"kind": "report", "message": "done"}
            assert steering.result(timeout=TIMEOUT).accepted
            assert adapter.respond({"time": 1}) == {"kind": "report", "message": "done"}


def test_failed_host_exits_without_waiting_for_runner_eof(tmp_path: Path) -> None:
    connector = tmp_path / "connector.py"
    connector.write_text(
        HOST.replace(
            "    await released.wait()\n", "    raise RuntimeError('native failure')\n"
        ).replace("steering=True, ", "")
    )
    requirements = HarnessRequirements(
        simulated_clock=False, chat_isolation=False, cancel=False, steering=False
    )

    with HarnessAdapter([sys.executable, str(connector)], TIMEOUT, None, requirements) as adapter:
        with pytest.raises(AdapterError, match="ended before response"):
            adapter.respond({"time": 0})
