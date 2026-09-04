"""One entry point over the engine's commands.

Each command already had its own `main()`, invoked as a module under
`fpl_agent.engine`, which is why the Makefile existed: to hide the module paths. A
single dispatcher gives `fpl-agent snapshot` instead. Those module entry points still
work and are deliberately kept for anyone who prefers them; the docs name the
`fpl-agent` form because it is the one that does not encode the package layout.
"""

import inspect
import sys
from typing import Callable, Optional

COMMANDS: dict[str, tuple[str, str]] = {
    "snapshot": ("fpl_agent.engine.snapshot", "Capture market, squad and lineups"),
    "project": ("fpl_agent.engine.projection", "Project expected points"),
    "rivals": ("fpl_agent.engine.rivals", "Capture rival squads from your leagues"),
    "recommend": ("fpl_agent.engine.recommend", "Rank transfers"),
    "settle": ("fpl_agent.engine.settle", "Grade projections against a finished gameweek"),
    "status": ("fpl_agent.engine.status", "Report whether the warehouse is trustworthy"),
    "brief": ("fpl_agent.engine.brief", "Write the gameweek brief to logs/gwNN.md"),
    "notify": ("fpl_agent.engine.notify", "Push the brief's triggers to ntfy, once each"),
    "serve": ("fpl_agent.main", "Run the MCP server"),
}


def _usage() -> str:
    width = max(len(name) for name in COMMANDS)
    lines = ["usage: fpl-agent <command> [options]", "", "commands:"]
    lines += [f"  {name:<{width}}  {help_}" for name, (_, help_) in COMMANDS.items()]
    lines += ["", "Run `fpl-agent <command> --help` for a command's own options."]
    return "\n".join(lines)


def main(argv: Optional[list[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ("-h", "--help"):
        print(_usage())
        return 0

    command = argv[0]
    if command not in COMMANDS:
        print(f"unknown command: {command}\n", file=sys.stderr)
        print(_usage(), file=sys.stderr)
        return 2

    module_name = COMMANDS[command][0]
    module = __import__(module_name, fromlist=["main"])
    entry: Callable = module.main
    # The engine commands parse their own arguments; the server takes none.
    if inspect.signature(entry).parameters:
        return entry(argv[1:]) or 0
    if argv[1:]:
        print(f"{command} takes no options", file=sys.stderr)
        return 2
    return entry() or 0


if __name__ == "__main__":
    raise SystemExit(main())
