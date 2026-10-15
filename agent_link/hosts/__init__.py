"""Host adapters: framework-neutral contracts plus one package per framework.

Importing this package has no side effects.  Use ``load_adapters(framework)``
(or a framework package's ``get_adapters()``) to obtain the ``HostAdapters``
pair; either role may be ``None`` when that framework does not implement it.
"""
from .base import ChildAdapter, HostAdapters, ParentAdapter, load_adapters

__all__ = ["ChildAdapter", "HostAdapters", "ParentAdapter", "load_adapters"]
