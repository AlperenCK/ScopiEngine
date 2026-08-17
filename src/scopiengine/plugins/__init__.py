"""Plugin discovery: four hooks (analyzer, ingest_processor, storage_backend, event),
loaded from built-ins, entry points and configured module paths.
"""

from __future__ import annotations

from scopiengine.plugins.hooks import EventBus
from scopiengine.plugins.registry import PluginLoadResult, PluginRegistry, discover_plugins

__all__ = ["EventBus", "PluginLoadResult", "PluginRegistry", "discover_plugins"]
