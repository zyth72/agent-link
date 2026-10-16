"""OpenCode adapters: native prompt HTTP push for parent and child roles."""
from ..base import HostAdapters
from .child import OpenCodeChildAdapter
from .parent import OpenCodeParentAdapter


def get_adapters() -> HostAdapters:
    return HostAdapters(parent=OpenCodeParentAdapter(), child=OpenCodeChildAdapter())
