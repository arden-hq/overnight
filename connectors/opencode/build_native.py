import hashlib
import io
import json
import os
import shutil
import subprocess
import tarfile
import tempfile
from pathlib import Path

import click
from overnight_opencode.launch import PATCHED_FILES

VERSION = "2.1.3"
SOURCE_COMMIT = "bdf757209ff742b8551a4874347edb845241e5bb"
SOURCE_PACKAGE_SHA256 = "5f8d39caf885db1c61c481a16df0acca0e383941e576ddb86d64e83e4c886a76"
SOURCE_LOCK_SHA256 = "c674592afda74f27cfe92cfa11a8a46c847701df06ec5e4b9f353d2f9d9b8252"
PATCHED_TREE_SHA256 = "ba38bc95a1286b5b1f19e0283a297312cf12c9d713eca7386d68497aa863a1fc"


def _inside(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def _safe_output(source: Path, out: Path) -> None:
    project = Path(__file__).resolve().parents[2]
    if out == project or out == source or _inside(source, out) or _inside(out, source):
        raise click.ClickException("source and output roots must be disjoint")
    if out.exists():
        raise click.ClickException("output must not already exist")


def _archive(source: Path, out: Path) -> str:
    commit = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=source, check=True, capture_output=True, text=True
    ).stdout.strip()
    if commit != SOURCE_COMMIT:
        raise click.ClickException(f"source must be pinned OpenCode commit {SOURCE_COMMIT}")
    archive = subprocess.run(
        ["git", "archive", "--format=tar", commit], cwd=source, check=True, capture_output=True
    ).stdout
    out.mkdir(parents=True, exist_ok=True, mode=0o700)
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:") as bundle:
        for member in bundle.getmembers():
            target = (out / member.name).resolve()
            if not _inside(target, out):
                raise click.ClickException("source archive contains an unsafe path")
            if member.issym() or member.islnk():
                link_target = (target.parent / member.linkname).resolve()
                if not _inside(link_target, out):
                    raise click.ClickException("source archive contains an escaping link")
        bundle.extractall(out, filter="data")
    return commit


def _tree_hash(out: Path, paths: list[str]) -> str:
    digest = hashlib.sha256()
    for relative in sorted(paths):
        digest.update(relative.encode())
        digest.update((out / relative).read_bytes())
    return digest.hexdigest()


@click.command()
@click.option("--source", required=True, type=click.Path(exists=True, path_type=Path))
@click.option("--out", required=True, type=click.Path(path_type=Path))
def main(source: Path, out: Path) -> None:
    source = source.resolve()
    out = out.resolve()
    _safe_output(source, out)
    out.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{out.name}.build-", dir=out.parent))
    try:
        source_commit = _archive(source, staging)
        package_bytes = (staging / "package.json").read_bytes()
        lock_bytes = (staging / "bun.lock").read_bytes()
        if (
            hashlib.sha256(package_bytes).hexdigest() != SOURCE_PACKAGE_SHA256
            or hashlib.sha256(lock_bytes).hexdigest() != SOURCE_LOCK_SHA256
        ):
            raise click.ClickException("archived OpenCode inputs do not match the pinned source")
        if json.loads(package_bytes).get("version") != VERSION:
            raise click.ClickException(f"archived source must be OpenCode {VERSION}")
        patch = Path(__file__).parent / "overnight_opencode/native-steering.patch"
        subprocess.run(
            ["/usr/bin/patch", "-p1", "--forward", "-i", str(patch.resolve())],
            cwd=staging,
            check=True,
        )
        subprocess.run(
            ["bun", "install", "--frozen-lockfile", "--ignore-scripts"], cwd=staging, check=True
        )
        patched = PATCHED_FILES
        subprocess.run(["bun", "x", "oxlint", *patched], cwd=staging, check=True)
        patched_tree = _tree_hash(staging, patched)
        if patched_tree != PATCHED_TREE_SHA256:
            raise click.ClickException("patched OpenCode tree does not match the reviewed output")
        manifest = {
            "protocol": 1,
            "version": VERSION,
            "source_commit": source_commit,
            "patch_sha256": hashlib.sha256(patch.read_bytes()).hexdigest(),
            "source_package_sha256": hashlib.sha256(package_bytes).hexdigest(),
            "source_lock_sha256": hashlib.sha256(lock_bytes).hexdigest(),
            "patched_tree_sha256": patched_tree,
        }
        (staging / "overnight-steering.json").write_text(json.dumps(manifest, indent=2) + "\n")
        os.replace(staging, out)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    click.echo(str(out))


if __name__ == "__main__":
    main()
