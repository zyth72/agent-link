"""Pi adapters: interactive bridge parent and RPC child (see docs/PI-RPC.md)."""
from ..base import HostAdapters
from .child import PiChildAdapter
from .parent import PiParentAdapter


def get_adapters() -> HostAdapters:
    return HostAdapters(parent=PiParentAdapter(), child=PiChildAdapter())
