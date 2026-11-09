from pathlib import Path

import click
from overnight_prime.lifecycle import require_evaluation_build
from overnight_prime.skills import install_bundled_skills


@click.command()
@click.option("--runtime", required=True, type=click.Path(exists=True, path_type=Path))
def main(runtime: Path) -> None:
    runtime = runtime.resolve()
    require_evaluation_build(runtime)
    install_bundled_skills(runtime)


if __name__ == "__main__":
    main()
