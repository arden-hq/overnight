import hashlib
import io
import json
import os
import shutil
import subprocess
import tarfile
from pathlib import Path

import click
from overnight_prime.skills import install_bundled_skills

COMMIT = "763094e1b6a7c17450ee8399b06cda1853847ca5"


@click.command()
@click.option("--source", required=True, type=click.Path(exists=True, path_type=Path))
@click.option(
    "--kernel",
    type=click.Path(exists=True, path_type=Path),
    help="An existing kernel venv to link; without it, the new binary bootstraps one in OUT.",
)
@click.option("--out", required=True, type=click.Path(path_type=Path))
@click.option("--toolchain", default="1.95.0", show_default=True)
@click.option("--jobs", default=4, show_default=True)
def main(source: Path, kernel: Path | None, out: Path, toolchain: str, jobs: int) -> None:
    """SOURCE is a Prime Agent git checkout containing the pinned release commit."""
    revision = subprocess.check_output(
        ["git", "-C", str(source), "rev-parse", COMMIT + "^{commit}"], text=True
    ).strip()
    if revision != COMMIT:
        raise click.ClickException("native source does not contain the pinned release")
    out = out.resolve()
    out.mkdir(parents=True, exist_ok=False)
    tree = out / "source"
    tree.mkdir()
    archive = subprocess.check_output(["git", "-C", str(source), "archive", COMMIT])
    with tarfile.open(fileobj=io.BytesIO(archive)) as files:
        files.extractall(tree, filter="data")
    patch = Path(__file__).parent / "overnight_prime/native-eval.patch"
    subprocess.run(["git", "apply", str(patch.resolve())], cwd=tree, check=True)
    subprocess.run(
        [
            "cargo",
            "+" + toolchain,
            "build",
            "--locked",
            "-p",
            "pa-cli",
            "--bin",
            "prime-agent",
            "-j",
            str(jobs),
        ],
        cwd=tree,
        check=True,
    )
    binary = out / "bin/prime-agent"
    binary.parent.mkdir()
    shutil.copy2(tree / "target/debug/prime-agent", binary)
    shutil.copytree(tree / "skills", binary.parent / "skills")
    if kernel is not None:
        (out / "kernel").symlink_to(kernel.resolve(), target_is_directory=True)
    else:
        shutil.copytree(tree / "prime-agent-runtime", binary.parent / "prime-agent-runtime")
        environment = os.environ | {
            "PRIME_AGENT_KERNEL_VENV": str(out / "kernel"),
            "PRIME_AGENT_TELEMETRY": "0",
        }
        subprocess.run([str(binary), "--prime-agent-bootstrap"], env=environment, check=True)
    install_bundled_skills(out)
    manifest = {
        "protocol": 1,
        "commit": COMMIT,
        "patch_sha256": hashlib.sha256(patch.read_bytes()).hexdigest(),
        "binary_sha256": hashlib.sha256(binary.read_bytes()).hexdigest(),
        "toolchain": toolchain,
        "profile": "dev",
    }
    (out / "eval-build.json").write_text(json.dumps(manifest, indent=2) + "\n")
    click.echo(str(out))


if __name__ == "__main__":
    main()
