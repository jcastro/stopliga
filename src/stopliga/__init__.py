"""StopLiga package; importing helpers does not load controller implementations."""

from typing import Sequence


def main(argv: Sequence[str] | None = None) -> int:
    from .cli import main as run

    return run(argv)


__all__ = ["main"]
