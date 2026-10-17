# agent_link test package (SPEC v0.1).

"""Shared helpers: keep test bindings out of the user's real registry."""

import os
import tempfile


def isolate_registry(test_case):
    """Point user-level Agent Link state at fresh temp dirs for one test case.

    CLI ``bind`` and the host service write/refresh user-level registry
    entries (default ``~/.agent-link/registry/``); tests must never write
    there.  Codex host/helper startup can also auto-register a connection from
    an inherited desktop pipe, so tests isolate that directory too and remove
    inherited transport variables.  Returns the registry temp dir path.
    """
    directory = tempfile.mkdtemp(prefix="agent-link-registry-")
    connections = tempfile.mkdtemp(prefix="agent-link-codex-connections-")
    previous = os.environ.get("AGENT_LINK_REGISTRY")
    previous_connections = os.environ.get("AGENT_LINK_CODEX_CONNECTIONS")
    previous_transport = {
        name: os.environ.get(name)
        for name in ("CODEX_APP_TOOLS_PIPE_PATH", "CODEX_APP_TOOLS_CALLER_HOST_ID")
    }

    def restore():
        if previous is None:
            os.environ.pop("AGENT_LINK_REGISTRY", None)
        else:
            os.environ["AGENT_LINK_REGISTRY"] = previous
        if previous_connections is None:
            os.environ.pop("AGENT_LINK_CODEX_CONNECTIONS", None)
        else:
            os.environ["AGENT_LINK_CODEX_CONNECTIONS"] = previous_connections
        for name, value in previous_transport.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value

    test_case.addCleanup(restore)
    os.environ["AGENT_LINK_REGISTRY"] = directory
    os.environ["AGENT_LINK_CODEX_CONNECTIONS"] = connections
    for name in previous_transport:
        os.environ.pop(name, None)
    return directory


def isolate_codex_connections(test_case):
    """Point AGENT_LINK_CODEX_CONNECTIONS at a fresh temp dir for one test."""
    directory = tempfile.mkdtemp(prefix="agent-link-codex-connections-")
    previous = os.environ.get("AGENT_LINK_CODEX_CONNECTIONS")

    def restore():
        if previous is None:
            os.environ.pop("AGENT_LINK_CODEX_CONNECTIONS", None)
        else:
            os.environ["AGENT_LINK_CODEX_CONNECTIONS"] = previous

    test_case.addCleanup(restore)
    os.environ["AGENT_LINK_CODEX_CONNECTIONS"] = directory
    return directory
