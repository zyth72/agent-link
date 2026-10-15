"""Codex desktop adapters: interactive parent only (no child adapter yet)."""
from ..base import HostAdapters
from .parent import CodexParentAdapter, build_codex_parent_prompt


def get_adapters() -> HostAdapters:
    return HostAdapters(parent=CodexParentAdapter(), child=None)
