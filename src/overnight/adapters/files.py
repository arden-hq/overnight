import os
from json import dump
from pathlib import Path

from overnight.domain.contracts import JsonObject


def write_private_json(path: Path, value: JsonObject, *, indent: int | None = None) -> None:
    """Exclusive creation prevents accidental overwrite of another run's settings."""
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as output:
        dump(value, output, ensure_ascii=False, allow_nan=False, indent=indent)


def replace_private_json(path: Path, value: JsonObject, *, indent: int | None = None) -> None:
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.unlink(missing_ok=True)
    write_private_json(temporary, value, indent=indent)
    temporary.replace(path)
