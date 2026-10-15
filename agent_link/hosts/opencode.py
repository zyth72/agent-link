"""OpenCode parent-side notification driver.

The transport is the OMP file bridge unchanged: ``notify()`` appends one
JSONL event per notification to ``<root>/omp-bridge/<binding_id>.jsonl``
(the shared bridge watcher resolves that directory name for the parent
role regardless of framework).  The OpenCode session runs
``extensions/opencode/agent-link.ts`` (OpenCode v2 plugin API), which is the
single consumer of the file and injects validated events into the tracked
session with ``ctx.session.prompt``.

Only the capability labels differ from OmpHost; append semantics, locking
and failure mapping (``DeliveryFailure`` -> ``failed_or_unknown``) are
inherited verbatim.
"""
from __future__ import annotations

from .omp import OmpHost


class OpenCodeHost(OmpHost):
    """OpenCode driver: same bridge file, opencode-labelled capabilities."""

    name = "opencode"

    def capabilities(self):
        capabilities = super().capabilities()
        capabilities.update(
            {
                "host": "opencode",
                "driver": "agent_link.hosts.opencode:OpenCodeHost",
                "notification_bridge": (
                    "<root>/omp-bridge/<binding_id>.jsonl + AGENT_LINK_BRIDGE_FILE watcher "
                    "(extensions/opencode/agent-link.ts)"
                ),
            }
        )
        return capabilities
