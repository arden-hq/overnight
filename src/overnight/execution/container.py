import os
import shutil
import subprocess
from pathlib import Path
from tempfile import gettempdir
from uuid import uuid4

import anyio

import overnight
from overnight.domain.contracts import AgentContainer
from overnight.execution.egress_proxy import READY

AGENT_DIRECTORY = "/overnight/agent"
# docker run's own exit code when it cannot create or start the container.
DOCKER_FAILED = 125
DOCKER = "docker"
INSPECT_SECONDS = 30.0
REMOVE_SECONDS = 15.0
AGENT_LABEL = "overnight.agent"
OWNER_LABEL = "overnight.owner"
EGRESS_ALIAS = "egress"
EGRESS_PORT = 3128
EGRESS_READY_SECONDS = 10.0
CONTAINER_PYTHON = "python3"


class ContainerUnavailable(Exception):
    def __init__(self, message: str, *, retryable: bool) -> None:
        super().__init__(message)
        self.retryable = retryable


def _repository() -> Path:
    return Path(overnight.__file__).resolve().parents[2]


def _overlaps(left: Path, right: Path) -> bool:
    return left == right or left in right.parents or right in left.parents


def check_mounts(spec: AgentContainer) -> None:
    repository = _repository()
    enclosing = [Path.home().resolve(), Path.cwd().resolve(), Path(gettempdir()).resolve()]
    for mount in spec.mounts:
        source = Path(mount.source).resolve()
        if not source.exists():
            raise ContainerUnavailable(f"mount {source} does not exist", retryable=False)
        if _overlaps(source, repository):
            raise ContainerUnavailable(
                f"mount {source} overlaps {repository}, which holds scenarios and runs",
                retryable=False,
            )
        for path in enclosing:
            if source == path or source in path.parents:
                raise ContainerUnavailable(f"mount {source} contains {path}", retryable=False)
        if mount.target == AGENT_DIRECTORY or mount.target.startswith(f"{AGENT_DIRECTORY}/"):
            raise ContainerUnavailable(f"mount target {mount.target} is reserved", retryable=False)


async def image_id(image: str) -> str:
    try:
        with anyio.fail_after(INSPECT_SECONDS):
            done = await anyio.run_process(
                [DOCKER, "image", "inspect", "--format", "{{.Id}}", image], check=False
            )
    except FileNotFoundError as error:
        raise ContainerUnavailable("docker is not installed", retryable=False) from error
    except TimeoutError as error:
        raise ContainerUnavailable("docker did not answer", retryable=True) from error
    if done.returncode == 0:
        return done.stdout.decode().strip()
    detail = done.stderr.decode().strip()
    if "No such image" in detail:
        raise ContainerUnavailable(f"container image {image} is not built", retryable=False)
    raise ContainerUnavailable(f"docker cannot inspect {image}: {detail}", retryable=True)


def keep_agent_folder(source: Path, target: Path) -> None:
    try:
        shutil.copytree(source, target, symlinks=True)
    except shutil.Error as error:
        lines = [f"{failed[0]}: {failed[2]}" for failed in error.args[0]]
        (target / "copy-errors.txt").write_text("\n".join(lines) + "\n")


def own_folder(folder: Path) -> None:
    if os.getuid() == 0:
        os.chown(folder, 65534, 65534)


def _user() -> str:
    uid, gid = os.getuid(), os.getgid()
    return "65534:65534" if uid == 0 else f"{uid}:{gid}"


def container_command(
    spec: AgentContainer,
    command: list[str],
    *,
    name: str,
    agent_directory: Path,
    variables: dict[str, str],
    network: str = "none",
) -> list[str]:
    arguments = [
        DOCKER,
        "run",
        "--interactive",
        "--rm",
        "--init",
        "--pull",
        "never",
        "--name",
        name,
        *_labels(),
        "--user",
        _user(),
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "--pids-limit",
        str(spec.pids),
        "--memory",
        spec.memory,
        "--cpus",
        str(spec.cpus),
        "--network",
        network,
        "--workdir",
        AGENT_DIRECTORY,
        "--mount",
        f"type=bind,source={agent_directory},target={AGENT_DIRECTORY}",
    ]
    for mount in spec.mounts:
        option = f"type=bind,source={Path(mount.source).resolve()},target={mount.target}"
        arguments += ["--mount", f"{option},readonly" if mount.read_only else option]
    for key, value in variables.items():
        arguments += ["--env", f"{key}={value}"]
    for key in spec.env:
        arguments += ["--env", key]
    return [*arguments, spec.image, *command]


def _labels() -> list[str]:
    return ["--label", f"{AGENT_LABEL}=1", "--label", f"{OWNER_LABEL}={os.getpid()}"]


async def _docker(*arguments: str) -> str:
    try:
        with anyio.fail_after(INSPECT_SECONDS):
            done = await anyio.run_process([DOCKER, *arguments], check=False)
    except TimeoutError as error:
        raise ContainerUnavailable("docker did not answer", retryable=True) from error
    if done.returncode != 0:
        detail = done.stderr.decode().strip()
        raise ContainerUnavailable(f"docker {arguments[0]} failed: {detail}", retryable=True)
    return done.stdout.decode()


def egress_names(name: str) -> tuple[str, str]:
    return f"{name}-net", f"{name}-egress"


def egress_variables() -> dict[str, str]:
    proxy = f"http://{EGRESS_ALIAS}:{EGRESS_PORT}"
    return {
        "HTTPS_PROXY": proxy,
        "https_proxy": proxy,
        "HTTP_PROXY": proxy,
        "http_proxy": proxy,
        "NO_PROXY": "localhost,127.0.0.1,::1",
        "no_proxy": "localhost,127.0.0.1,::1",
        # Node's fetch ignores the proxy variables unless told to read them.
        "NODE_USE_ENV_PROXY": "1",
    }


def network_environment() -> dict[str, str]:
    return {key: os.environ[key] for key in egress_variables() if key in os.environ}


async def start_egress(name: str, spec: AgentContainer) -> str:
    network, proxy = egress_names(name)
    await _docker("network", "create", "--internal", *_labels(), network)
    allow = [part for host in spec.allow_hosts for part in ("--allow", host)]
    await _docker(
        "run",
        "--detach",
        "--pull",
        "never",
        "--name",
        proxy,
        *_labels(),
        "--user",
        "65534:65534",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "--read-only",
        "--pids-limit",
        "64",
        "--memory",
        "256m",
        "--log-opt",
        "max-size=20m",
        "--network",
        "bridge",
        spec.image,
        "python3",
        "-m",
        "overnight.execution.egress_proxy",
        "--port",
        str(EGRESS_PORT),
        *allow,
    )
    await _docker("network", "connect", "--alias", EGRESS_ALIAS, network, proxy)
    with anyio.move_on_after(EGRESS_READY_SECONDS):
        while READY not in await _docker("logs", proxy):
            await anyio.sleep(0.1)
        return network
    raise ContainerUnavailable("egress proxy did not start", retryable=True)


async def stop_egress(name: str, log: Path | None) -> None:
    network, proxy = egress_names(name)
    if log is not None:
        with anyio.move_on_after(REMOVE_SECONDS), log.open("wb") as output:
            await anyio.run_process(
                [DOCKER, "logs", proxy], check=False, stdout=subprocess.DEVNULL, stderr=output
            )
    await remove(proxy)
    await _remove_network(network)


async def _remove_network(network: str) -> None:
    with anyio.move_on_after(REMOVE_SECONDS):
        await anyio.run_process(
            [DOCKER, "network", "rm", network],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )


def new_name() -> str:
    return f"overnight-agent-{uuid4().hex[:16]}"


async def remove(name: str) -> None:
    with anyio.move_on_after(REMOVE_SECONDS):
        await anyio.run_process(
            [DOCKER, "rm", "--force", name],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


async def sweep() -> None:
    if shutil.which(DOCKER) is None:
        return
    owner = f'{{{{.Label "{OWNER_LABEL}"}}}}'
    with anyio.move_on_after(INSPECT_SECONDS):
        for kind, removal in (("ps --all", remove), ("network ls", _remove_network)):
            listed = await anyio.run_process(
                [
                    DOCKER,
                    *kind.split(),
                    "--filter",
                    f"label={AGENT_LABEL}=1",
                    "--format",
                    f"{{{{.Name}}}} {owner}" if kind == "network ls" else f"{{{{.Names}}}} {owner}",
                ],
                check=False,
            )
            for line in listed.stdout.decode().splitlines():
                name, _, pid = line.partition(" ")
                if pid.isdigit() and not _alive(int(pid)):
                    await removal(name)
