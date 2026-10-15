#!/usr/bin/env python3
"""Run the locally installed Agent Link implementation from any cwd."""
import json
import os
from pathlib import Path
import sys


def _repo_root() -> Path:
    """Locate the repository root.

    AGENT_LINK_REPO wins when set (needed when this script is installed as a
    standalone skill copy).  Otherwise derive the root from the repository
    layout: <repo>/skills/agent-link/scripts/link.py.
    """
    override = os.environ.get("AGENT_LINK_REPO")
    if override:
        return Path(override).expanduser().resolve()
    return Path(__file__).resolve().parents[3]


def main():
    repo = _repo_root()
    if not (repo / "agent_link" / "__main__.py").is_file():
        message = f"Agent Link implementation missing at {repo}; set AGENT_LINK_REPO to its repository root"
        print(message, file=sys.stderr)
        print(json.dumps({"error": "implementation_missing", "message": message}))
        return 1
    sys.path.insert(0, str(repo))
    from agent_link.cli import main as cli_main
    return cli_main()


if __name__ == "__main__":
    raise SystemExit(main())
