# agent_link test package (SPEC v0.1).

"""Shared helpers: keep test bindings out of the user's real registry."""

import os
import tempfile


def isolate_registry(test_case):
    """Point AGENT_LINK_REGISTRY at a fresh temp dir for one test case.

    CLI ``bind`` and the host service write/refresh user-level registry
    entries (default ``~/.agent-link/registry/``); tests must never write
    there.  Returns the temp dir path.
    """
    directory = tempfile.mkdtemp(prefix="agent-link-registry-")
    previous = os.environ.get("AGENT_LINK_REGISTRY")

    def restore():
        if previous is None:
            os.environ.pop("AGENT_LINK_REGISTRY", None)
        else:
            os.environ["AGENT_LINK_REGISTRY"] = previous

    test_case.addCleanup(restore)
    os.environ["AGENT_LINK_REGISTRY"] = directory
    return directory
