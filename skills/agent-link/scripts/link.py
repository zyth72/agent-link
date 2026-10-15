#!/usr/bin/env python3
"""Run the locally installed Agent Link implementation from any cwd.

Default dispatch: the original CLI (``agent_link.cli``).  With ``--codex`` as
the first argument the Codex desktop parent helper (``agent_link.codex``)
handles the remaining arguments, so a skill wrapper copied outside the
repository can offer both entry points.

Repository location, in order:

1. ``AGENT_LINK_REPO`` when set;
2. the repository layout of this file (``<repo>/skills/agent-link/scripts/link.py``);
3. ``repo-location.json`` beside this script, written by the local installer
   for a standalone skill copy.  That file is a machine-local install artifact
   and is not committed.
"""
import json
import os
from pathlib import Path
import sys

LOCATION_FILENAME = "repo-location.json"


def _configured_repo(script_dir: Path):
    """Read the installer-written absolute repository path, if present."""
    location = script_dir / LOCATION_FILENAME
    try:
        data = json.loads(location.read_text("utf-8"))
    except (OSError, ValueError):
        return None
    repo = data.get("repo") if isinstance(data, dict) else None
    if isinstance(repo, str) and repo.strip():
        return Path(repo).expanduser().resolve()
    return None


def _repo_root() -> Path:
    override = os.environ.get("AGENT_LINK_REPO")
    if override:
        return Path(override).expanduser().resolve()
    script_dir = Path(__file__).resolve().parent
    derived = script_dir.parents[2]
    if (derived / "agent_link" / "__main__.py").is_file():
        return derived
    configured = _configured_repo(script_dir)
    if configured is not None:
        return configured
    return derived


def main():
    repo = _repo_root()
    if not (repo / "agent_link" / "__main__.py").is_file():
        message = (
            f"Agent Link implementation missing at {repo}; set AGENT_LINK_REPO or write "
            f"{LOCATION_FILENAME} beside this script with the repository root"
        )
        print(message, file=sys.stderr)
        print(json.dumps({"error": "implementation_missing", "message": message}))
        return 1
    sys.path.insert(0, str(repo))
    argv = sys.argv[1:]
    if argv and argv[0] == "--codex":
        from agent_link.codex import main as codex_main
        return codex_main(argv[1:])
    from agent_link.cli import main as cli_main
    return cli_main()


if __name__ == "__main__":
    raise SystemExit(main())
