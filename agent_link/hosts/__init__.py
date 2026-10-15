"""Host drivers implement capabilities() and notify(root, binding, message)."""
from typing import Protocol


class HostDriver(Protocol):
    def capabilities(self) -> dict: ...
    def notify(self, root, binding: dict, message: dict) -> str: ...
