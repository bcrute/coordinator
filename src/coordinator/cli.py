"""Coordinator command-line interface."""

from __future__ import annotations

import argparse
import sys

from . import __version__


COMMANDS = ("serve", "doctor", "init", "data", "run-turn")


def _help() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="coordinator",
        description="Run and inspect file-backed, frontier-led coding workflows.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("command", nargs="?", choices=COMMANDS)
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    parser = _help()
    if not arguments or arguments[0] in {"-h", "--help"}:
        parser.print_help()
        return 0
    if arguments[0] in {"-V", "--version"}:
        print(f"coordinator {__version__}")
        return 0

    command, rest = arguments[0], arguments[1:]
    if command == "serve":
        from .web_app import main as serve

        return serve(rest)
    if command == "doctor":
        from .doctor import main as doctor

        return doctor(rest)
    if command == "init":
        from .init_project import main as initialize

        return initialize(rest)
    if command == "data":
        from .maintenance import main as maintain

        return maintain(rest)
    if command == "run-turn":
        from .run_executor_turn import main as run_turn

        return run_turn(rest)
    parser.error(f"unknown command: {command}")
    return 2
