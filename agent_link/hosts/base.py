"""Framework-neutral host adapter contracts and package registry.

Every framework directory implements ``get_adapters() -> HostAdapters``:
``parent`` is the interactive-session notification adapter (or ``None`` when
that role is unsupported) and ``child`` is the delivery adapter for tasks
delegated to that framework (or ``None``).  An entry object only proves the
role is implemented; it never proves a framework process is connected.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
import importlib

from ..errors import ValidationError

#: Framework name -> importable package providing ``get_adapters()``.
FRAMEWORK_MODULES = {
    "codex-desktop": "agent_link.hosts.codex",
    "pi": "agent_link.hosts.pi",
    "omp": "agent_link.hosts.omp",
    "opencode": "agent_link.hosts.opencode",
}


class ParentAdapter(ABC):
    """Deliver notifications to an interactive parent session."""

    framework = ""

    @abstractmethod
    def capabilities(self) -> dict:
        """Declare transport, busy behavior and unverified capabilities."""

    @abstractmethod
    def notify(self, root, binding: dict, message: dict) -> str:
        """Deliver one report/ask message; return an audit detail string."""


class ChildAdapter(ABC):
    """Deliver parent-initiated messages to a child session/task."""

    framework = ""

    @abstractmethod
    def capabilities(self) -> dict:
        """Declare transport and delivery semantics; unknowns stay unknown."""

    @abstractmethod
    def deliver(self, root, binding: dict, message: dict) -> str:
        """Deliver one ask_child message; return an audit detail string."""


@dataclass(frozen=True)
class HostAdapters:
    """Both roles for one framework, either side possibly ``None``."""

    parent: ParentAdapter | None
    child: ChildAdapter | None


def load_adapters(framework: str) -> HostAdapters:
    """Load the registered adapters for one framework name.

    Importing an adapter package must not start processes, open sockets or
    read credentials; only the explicit controller/service entry points do.
    """
    if not isinstance(framework, str) or not framework.strip():
        raise ValidationError(f"framework must be a non-empty string, got {framework!r}")
    module_name = FRAMEWORK_MODULES.get(framework)
    if module_name is None:
        raise ValidationError(
            f"unknown framework {framework!r}; registered: {sorted(FRAMEWORK_MODULES)}"
        )
    module = importlib.import_module(module_name)
    adapters = module.get_adapters()
    if not isinstance(adapters, HostAdapters):
        raise ValidationError(f"{module_name}.get_adapters() must return HostAdapters")
    return adapters
