"""OpenCode adapters: bridge-file parent and legacy inbox child."""
from ..base import HostAdapters
from .child import OpenCodeChildAdapter
from .parent import OpenCodeParentAdapter


def get_adapters() -> HostAdapters:
    return HostAdapters(parent=OpenCodeParentAdapter(), child=OpenCodeChildAdapter())
