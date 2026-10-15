"""OMP adapters: bridge-file parent and legacy inbox child."""
from ..base import HostAdapters
from .child import OmpChildAdapter
from .parent import OmpParentAdapter


def get_adapters() -> HostAdapters:
    return HostAdapters(parent=OmpParentAdapter(), child=OmpChildAdapter())
