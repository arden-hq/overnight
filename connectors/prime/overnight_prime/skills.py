import asyncio
import subprocess
from pathlib import Path

from overnight_prime.runtime import native_environment


def python_skill_packages(skills: list[Path]) -> list[Path]:
    return [skill.parent for skill in skills if (skill.parent / "pyproject.toml").is_file()]


def install_bundled_skills(runtime: Path) -> None:
    kernel = runtime / "kernel"
    if not (kernel / "pyvenv.cfg").is_file():
        raise ValueError(
            "native skills must be installed into a private Python virtual environment"
        )
    packages = python_skill_packages(sorted((runtime / "bin/skills").glob("*/SKILL.md")))
    subprocess.run(
        ["uv", "pip", "install", "--python", str(kernel / "bin/python"), *map(str, packages)],
        check=True,
    )


async def verify_python_skills(python: Path, skills: list[Path]) -> None:
    modules = [package.name.replace("-", "_") for package in python_skill_packages(skills)]
    process = await asyncio.create_subprocess_exec(
        str(python),
        "-c",
        "import importlib,sys; [importlib.import_module(name) for name in sys.argv[1:]]",
        *modules,
        env=native_environment({}),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        async with asyncio.timeout(10):
            _, stderr = await process.communicate()
    except BaseException:
        if process.returncode is None:
            process.kill()
        await process.wait()
        raise
    if process.returncode != 0:
        detail = stderr.decode(errors="replace").strip().splitlines()[-1:]
        raise ValueError(
            "native Python skills are unavailable; run connectors/prime/install_skills.py "
            "for this runtime. " + " ".join(detail)
        )
